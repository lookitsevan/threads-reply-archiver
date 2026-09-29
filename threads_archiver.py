#!/usr/bin/env python3
"""
Threads Reply Archiver
----------------------
Append-only archive of replies on YOUR OWN Threads posts, meant to run every 15 min.

- Identity comes only from the token in config.env (no handles in code).
- state.json is the source of truth; archive.csv is regenerated from it each run.
- Your `tag` and `notes` columns in archive.csv are preserved across runs.
- Replies that disappear are kept and marked status=missing (never deleted).
- Every capture is also written untouched to data/raw/*.jsonl with a SHA-256 hash.
- Media attachments are downloaded to data/media/ while still available, so a
  deleted image/video reply still has evidence on disk.

Hardened 2026-09-28 from an independent dual code audit:
- pagination tracks completeness; a reply is only marked missing after a
  fully completed scan (empty pages are followed through, cursor loops detected)
- the 30-day lookback is for post DISCOVERY only; every archived post is
  rechecked each run, including posts older than the window and replies under
  deleted/inaccessible root posts (which get an explicit inaccessible state)
- state is committed after each post, so a late-run crash keeps earlier work
- per-run scan accounting (attempted/completed/failed/inaccessible) with
  fail-fast on account-wide auth failures
- all transport failures (timeouts, truncated reads, bad JSON) normalize into
  one ApiError type with structured Graph error codes
- degraded reply-field sets persist across runs and heal when the API recovers
- token persistence handles disk errors instead of stranding a refreshed token

Standard library only (no installs needed).
"""
import csv, fcntl, hashlib, json, os, re, shutil, socket, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force IPv4: Python doesn't fall back from a broken IPv6 route the way browsers do,
# which shows up as connections that hang forever at sock.connect.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
socket.getaddrinfo = _ipv4_only

BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / "config.env"
DATA = ROOT / "data"
RAW = DATA / "raw"
MEDIA_DIR = DATA / "media"
STATE_PATH = DATA / "state.json"
CSV_PATH = DATA / "archive.csv"
CSV_BAK = DATA / "archive.csv.bak"
LOG_PATH = DATA / "log.txt"
LOCK_PATH = DATA / ".lock"

POST_FIELDS = "id,text,timestamp,permalink,media_type,username"
# Base reply field set. Fields the API rejects are dropped one at a time and the
# degraded set persists in state.json (it heals when the API accepts them again).
REPLY_FIELDS_BASE = [
    "id", "text", "username", "permalink", "timestamp", "media_type",
    "has_replies", "is_reply", "replied_to", "root_post", "hide_status",
    "media_url", "thumbnail_url", "gif_url",
    "children{media_url,media_type,thumbnail_url}",
]
REPLY_FIELDS_MIN = ["id", "text", "username", "permalink", "timestamp"]  # never dropped

# hide_status values (UNHUSHED is Meta's spelling). Only the HIDDEN_* set means
# the reply is suppressed from public view; everything else is treated as live.
HIDE_STATUS_HIDDEN = {"HIDDEN", "COVERED", "BLOCKED", "RESTRICTED"}
HIDE_STATUS_VISIBLE = {"", "NOT_HUSHED", "UNHUSHED", "NOT_HIDDEN"}

MAX_MEDIA_PER_RUN = 50       # downloaded attachments per run (evidence copies)
MAX_MEDIA_BYTES = 20 * 1024 * 1024
RUN_DEADLINE_S = 20 * 60     # stop scanning after this; finalize the partial run
FUTURE_SKEW_S = 24 * 3600    # timestamps further in the future are clock skew
MAX_EVENTS = 20000           # append-only event journal cap (oldest trimmed)

CSV_COLUMNS = [
    # --- which post (group by this) ---
    "post_label", "post_created_at", "post_text", "post_permalink",
    # --- the reply ---
    "reply_created_at", "username", "text", "reply_type", "status",
    # --- yours to fill in ---
    "tag", "notes",
    # --- history / evidence ---
    "edited", "original_text", "reply_permalink", "hide_status",
    "first_captured_at", "last_seen_at", "missing_since",
    "post_id", "reply_id", "replied_to_id", "sha256_first_capture",
    # --- media evidence ---
    "media_files", "media_sha256",
]
USER_COLUMNS = ("tag", "notes")                # columns YOU edit; never overwritten
TEXT_COLUMNS = ("post_label", "username", "text", "post_text", "original_text")

TOKEN = ""


class ApiError(Exception):
    """Single normalized transport/API error type."""

    def __init__(self, message, *, http_code=None, graph_code=None,
                 graph_type=None, graph_subcode=None):
        super().__init__(message)
        self.http_code = http_code
        self.graph_code = graph_code
        self.graph_type = graph_type
        self.graph_subcode = graph_subcode


def utcnow():
    return datetime.now(timezone.utc)


def now_iso():
    return utcnow().replace(microsecond=0).isoformat()


def sane_timestamp(ts):
    """Reject implausible future timestamps (clock skew); '' if unusable."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt - utcnow()).total_seconds() > FUTURE_SKEW_S:
        log(f"WARNING: implausible future timestamp {ts}; storing empty")
        return ""
    return str(ts)


def redact(s):
    return s.replace(TOKEN, "***TOKEN***") if TOKEN else s


def log(msg):
    line = f"{now_iso()}  {redact(str(msg))}"
    print(line)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging must never crash a run


def die(msg):
    try:
        log(f"ERROR: {msg}")
    except OSError:
        pass
    sys.exit(1)


# ---------- config ----------
def load_env():
    if not ENV_PATH.exists():
        die("config.env not found. Double-click Start.command to create it.")
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env_value(key, value):
    lines, found = [], False
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key + "="):
            lines.append(f"{key}={value}")
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    tmp = ROOT / "config.env.tmp"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)


# ---------- HTTP ----------
def parse_graph_error(body):
    """Structured (code, type, subcode) from a Graph error body, or Nones."""
    try:
        err = json.loads(body).get("error") or {}
    except Exception:
        return None, None, None
    return err.get("code"), err.get("type"), err.get("error_subcode")


def api_get(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "threads-reply-archiver/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()                       # may raise socket.timeout
                return json.loads(body.decode("utf-8"))  # may raise JSONDecodeError
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                raw = "<unreadable body>"
            gcode, gtype, gsub = parse_graph_error(raw)
            err = ApiError(f"HTTP {e.code}: {redact(raw[:300])}",
                           http_code=e.code, graph_code=gcode,
                           graph_type=gtype, graph_subcode=gsub)
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise err
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # normalized: every transport-level failure becomes an ApiError
            err = ApiError(f"transport failure: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise err


def paginate(path, params, max_pages=100):
    """Follow paging cursors; track completeness (loops / truncation).

    Returns (items, complete, note). An empty page does NOT end the scan when a
    'next' cursor exists; cursor loops and page caps mark the scan incomplete.
    """
    url = f"{BASE}/{path}"
    q = dict(params, access_token=TOKEN)
    items, seen_urls = [], set()
    empty_streak = 0
    for _ in range(max_pages):
        if url in seen_urls:
            return items, False, "cursor loop detected"
        seen_urls.add(url)
        data = api_get(url, q)
        q = None                                  # 'next' URLs already carry params
        batch = data.get("data", [])
        if batch:
            empty_streak = 0
            items.extend(batch)
        else:
            empty_streak += 1
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            return items, True, ""                # clean end: no further cursor
        if empty_streak >= 5:
            return items, False, "too many consecutive empty pages"
        url = nxt
    return items, False, f"hit max_pages={max_pages}"


_FIELD_RE = re.compile(
    r"(?:nonexisting field|unknown field|invalid field)\s*\(?['\"]?"
    r"([a-zA-Z_][a-zA-Z0-9_]*)['\"]?\)?",
    re.IGNORECASE)


def current_reply_fields(state):
    """Field set minus anything the API rejected (persisted in state.json)."""
    dropped = set(state.get("fields_dropped", []))
    return ",".join(f for f in REPLY_FIELDS_BASE if f not in dropped)


def fetch_conversation(post_id, state):
    """(items, complete, note, fields_used). Drops only rejected fields."""
    fields = current_reply_fields(state)
    try:
        items, complete, note = paginate(
            f"{post_id}/conversation", {"fields": fields, "reverse": "false"})
        return items, complete, note, fields
    except ApiError as e:
        m = _FIELD_RE.search(str(e))
        field = m.group(1) if m else None
        dropped = state.setdefault("fields_dropped", [])
        if (field and field in REPLY_FIELDS_BASE
                and field not in REPLY_FIELDS_MIN and field not in dropped):
            dropped.append(field)
            save_state(state)                     # persist degradation now
            log(f"Field '{field}' rejected by the API; dropping it for future "
                f"runs (it will be retried when the API accepts it again).")
            fields = current_reply_fields(state)
            items, complete, note = paginate(
                f"{post_id}/conversation", {"fields": fields, "reverse": "false"})
            return items, complete, note, fields
        raise


# ---------- token upkeep ----------
def maybe_refresh_token(state):
    """Long-lived tokens last 60 days; refresh weekly so it never lapses."""
    global TOKEN
    try:
        recent = (state.get("token_refreshed_at") and
                  datetime.fromisoformat(state["token_refreshed_at"])
                  > utcnow() - timedelta(days=7))
    except ValueError:
        recent = False                            # garbage timestamp: refresh
    if recent:
        return
    tried = state.get("token_refresh_attempted_at")
    try:
        if tried and datetime.fromisoformat(tried) > utcnow() - timedelta(days=1):
            return
    except ValueError:
        pass
    state["token_refresh_attempted_at"] = now_iso()
    try:
        data = api_get(REFRESH_URL, {"grant_type": "th_refresh_token", "access_token": TOKEN})
        new = data.get("access_token")
        if new:
            try:
                save_env_value("THREADS_ACCESS_TOKEN", new)
            except OSError as e:
                # Meta issued a token but we couldn't store it: say so loudly
                # instead of silently stranding it.
                die(f"Token was refreshed but config.env could not be written: {e}. "
                    f"Your old token may still work; re-run after fixing the disk error.")
            TOKEN = new
            state["token_refreshed_at"] = now_iso()
            try:
                save_state(state)                 # persist now so a crash doesn't re-refresh
            except OSError as e:
                die(f"Token saved to config.env but state.json could not be written: {e}.")
            log("Access token refreshed.")
    except ApiError as e:
        log(f"Token refresh skipped (normal if token is under 24h old): {e}")


# ---------- storage ----------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"replies": {}, "posts": {}, "fields_dropped": [], "events": []}


def save_state(state):
    tmp = DATA / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def record_event(state, at, reply_id, post_id, etype, detail=""):
    events = state.setdefault("events", [])
    events.append({"at": at, "reply_id": reply_id, "post_id": post_id,
                   "type": etype, "detail": detail})
    if len(events) > MAX_EVENTS:
        del events[:len(events) - MAX_EVENTS]


def status_of(rec):
    """Status as shown in the CSV. 'missing' is disappearance from the API,
    not proven deletion (a block, private account, or Meta removal looks the same)."""
    if rec.get("missing_since"):
        return "missing"
    if (rec.get("hide_status") or "") in HIDE_STATUS_HIDDEN:
        return "live (hidden)"
    return "live"


def csv_safe(value):
    """Block spreadsheet formula injection, including leading-whitespace
    and leading-newline evasion (e.g. ' =HYPERLINK(...)' would execute)."""
    s = "" if value is None else str(value)
    return "'" + s if s.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@") else s


def write_csv(records):
    kept = {}
    if CSV_PATH.exists():
        try:
            with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                cols = set(reader.fieldnames or [])
                if {"reply_id", "tag", "notes"} <= cols:
                    for row in reader:
                        rid = (row.get("reply_id") or "").strip()
                        # only keep annotations for replies we actually archive
                        if rid and rid in records:
                            kept[rid] = {k: row.get(k, "") or "" for k in USER_COLUMNS}
        except (OSError, csv.Error) as e:
            log(f"WARNING: could not read existing CSV annotations: {e}; "
                f"keeping none this run")
    rows = []
    for rid, rec in records.items():
        row = {c: rec.get(c, "") for c in CSV_COLUMNS}
        row["reply_id"] = rid
        row["status"] = status_of(rec)
        snippet = " ".join((rec.get("post_text") or "(no text)").split())[:50]
        row["post_label"] = f"{(rec.get('post_created_at') or '')[:10]} · {snippet}"
        parent = rec.get("replied_to_id", "")
        if not parent or parent == rec.get("post_id"):
            row["reply_type"] = "reply to post"
        else:
            who = records.get(parent, {}).get("username", "")
            row["reply_type"] = f"↳ reply to @{who}" if who else "↳ nested reply"
        media = rec.get("media", [])
        row["media_files"] = ";".join(m.get("path", "") for m in media if m.get("path"))
        row["media_sha256"] = ";".join(m.get("sha256", "") for m in media if m.get("sha256"))
        for c in TEXT_COLUMNS:
            row[c] = csv_safe(row[c])
        row.update(kept.get(rid, {}))
        rows.append(row)
    # replies oldest-first inside each post, posts newest-first (stable two-pass sort)
    rows.sort(key=lambda r: r["reply_created_at"])
    rows.sort(key=lambda r: (r["post_created_at"], r["post_id"]), reverse=True)
    if CSV_PATH.exists():
        try:
            shutil.copy2(CSV_PATH, CSV_BAK)       # back up before replacing
        except OSError as e:
            log(f"WARNING: could not back up archive.csv: {e}")
    tmp = DATA / "archive.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:   # -sig so Excel shows emoji
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CSV_PATH)
    return len(rows)


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------- media evidence ----------
CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
          "image/gif": ".gif", "video/mp4": ".mp4", "video/quicktime": ".mov"}


def collect_media_urls(r):
    """Candidate attachment URLs from a reply payload (incl. carousel children)."""
    urls = []
    for k in ("media_url", "thumbnail_url", "gif_url"):
        if r.get(k):
            urls.append(r[k])
    for child in (r.get("children") or {}).get("data", []) or []:
        for k in ("media_url", "thumbnail_url"):
            if child.get(k):
                urls.append(child[k])
    return list(dict.fromkeys(urls))              # de-dupe, preserve order


def download_media(url, budget):
    """Fetch attachment bytes while they're still available.

    Returns (rel_path|None, sha256|None, error|None). Never raises.
    """
    if budget["count"] >= MAX_MEDIA_PER_RUN:
        return None, None, "per-run media cap reached"
    req = urllib.request.Request(url, headers={"User-Agent": "threads-reply-archiver/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            ext = CT_EXT.get(ctype)
            if not ext:
                ext = os.path.splitext(urllib.parse.urlparse(url).path)[1][:5] or ".bin"
            chunks, total = [], 0
            while True:
                c = r.read(65536)
                if not c:
                    break
                total += len(c)
                if total > MAX_MEDIA_BYTES:
                    return None, None, "exceeds 20MB cap"
                chunks.append(c)
            data = b"".join(chunks)
    except Exception as e:                        # recorded, never raised
        return None, None, f"{type(e).__name__}: {e}"
    digest = hashlib.sha256(data).hexdigest()
    dest = MEDIA_DIR / f"{digest[:16]}{ext}"
    try:
        if not dest.exists():
            dest.write_bytes(data)
    except OSError as e:
        return None, None, f"disk write failed: {e}"
    budget["count"] += 1
    return str(dest.relative_to(ROOT)), digest, None


# ---------- main scan ----------
def run():
    global TOKEN
    env = load_env()
    TOKEN = env.get("THREADS_ACCESS_TOKEN", "")
    if not TOKEN or TOKEN.startswith("paste"):
        die("No token in config.env yet. Paste it after THREADS_ACCESS_TOKEN= and save.")
    lookback = int(env.get("LOOKBACK_DAYS", "30") or 30)

    state = load_state()
    state.setdefault("replies", {})
    state.setdefault("posts", {})
    state.setdefault("fields_dropped", [])
    state.setdefault("events", [])
    if state.get("run_open"):
        log("Previous run did not finish cleanly; per-post commits preserved "
            "whatever was scanned before the crash.")
    state["run_open"] = True
    save_state(state)
    try:
        _scan(state, lookback)
    finally:
        state["run_open"] = False
        try:
            save_state(state)
        except OSError as e:
            log(f"WARNING: could not write final state: {e}")


def _scan(state, lookback):
    print("Contacting Threads...")
    maybe_refresh_token(state)

    # identity gate: fail fast on account-wide auth problems
    try:
        me = api_get(f"{BASE}/me", {"fields": "id,username", "access_token": TOKEN})
    except ApiError as e:
        if e.http_code in (400, 401, 403) or e.graph_code == 190:
            die(f"Threads rejected this account's token ({e}). Generate a new "
                f"token and paste it into config.env.")
        raise
    if state.get("account_id") and state["account_id"] != me["id"]:
        die("This archive belongs to a different Threads account. Use a fresh copy of the folder.")
    state["account_id"], state["username"] = me["id"], me.get("username", "")
    save_state(state)

    # field-set recovery: if the API rejected fields before, try the full set
    # again once a day; success heals the degraded set (and backfills below).
    dropped = state.get("fields_dropped", [])
    recovered_at = state.get("fields_recovered_at")
    recover_due = (dropped and
                   (not recovered_at or recovered_at < (utcnow() - timedelta(days=1)).isoformat()))
    if recover_due:
        state["fields_dropped"] = []
        state["fields_recovered_at"] = now_iso()
        save_state(state)
        log("Trying the full reply field set again (it was degraded before).")

    deadline = time.monotonic() + RUN_DEADLINE_S
    run_at = now_iso()
    run = {"started_at": run_at, "posts_attempted": 0, "posts_completed": 0,
           "posts_failed": 0, "posts_inaccessible": 0, "new": 0, "edited": 0,
           "newly_missing": 0, "reappeared": 0}

    # 1. DISCOVERY: the lookback window finds NEW posts; it does not bound
    #    which posts get rechecked.
    since = int((utcnow() - timedelta(days=lookback)).timestamp())
    print(f"Signed in as @{me.get('username','?')}. Discovering posts from the last {lookback} days...")
    posts, disc_complete, disc_note = paginate("me/threads", {"fields": POST_FIELDS, "since": since})
    if not disc_complete:
        log(f"WARNING: post discovery pagination incomplete ({disc_note}); "
            f"new posts may be missed this run")
    posts_state = state["posts"]
    for p in posts:
        pid = p["id"]
        rec = posts_state.get(pid, {})
        rec.update({
            "text": p.get("text", ""), "created_at": sane_timestamp(p.get("timestamp", "")),
            "permalink": p.get("permalink", ""), "media_type": p.get("media_type", ""),
            "username": p.get("username", ""),
            "first_seen_at": rec.get("first_seen_at", run_at),
            "check_status": rec.get("check_status", "ok"),
            "check_error": rec.get("check_error", ""),
            "last_checked_at": rec.get("last_checked_at", ""),
        })
        posts_state[pid] = rec
    print(f"Tracking {len(posts_state)} posts ({len(posts)} in the discovery window). Checking replies...")
    save_state(state)

    # 2. RECHECK: every archived post, oldest-checked first (incl. >30d old)
    records = state["replies"]
    by_post = {}
    for rid, rec in records.items():
        by_post.setdefault(rec.get("post_id", ""), []).append(rid)
    ordered = sorted(posts_state, key=lambda pid: posts_state[pid].get("last_checked_at") or "")
    media_budget = {"count": 0}

    raw_path = RAW / f"{run_at[:10]}.jsonl"
    try:
        raw = raw_path.open("a", encoding="utf-8")
    except OSError as e:
        die(f"Cannot write raw evidence file {raw_path}: {e}")

    try:
        for i, pid in enumerate(ordered, 1):
            if time.monotonic() > deadline:
                log("Run deadline reached; stopping the scan and finalizing "
                    "the partial run (nothing is lost — next run continues).")
                break
            run["posts_attempted"] += 1
            try:
                replies, complete, note, fields_used = fetch_conversation(pid, state)
            except ApiError as e:
                # deleted/inaccessible root vs transient failure: only the first
                # gets an 'inaccessible' state; transient failures retry next run
                if e.http_code == 404 or (e.graph_code == 100 and
                                          e.graph_subcode in (33, 210)):
                    posts_state[pid]["check_status"] = "inaccessible"
                    posts_state[pid]["check_error"] = str(e)[:300]
                    posts_state[pid]["last_checked_at"] = run_at
                    record_event(state, run_at, "", pid, "post_inaccessible", str(e)[:300])
                    run["posts_inaccessible"] += 1
                    log(f"Post {pid}: inaccessible ({e}) — its replies keep their last status.")
                else:
                    posts_state[pid]["check_status"] = "failed"
                    posts_state[pid]["check_error"] = str(e)[:300]
                    posts_state[pid]["last_checked_at"] = run_at
                    run["posts_failed"] += 1
                    log(f"Post {pid}: skipped this run, nothing marked missing ({e}).")
                save_state(state)                 # per-post commit, even on failure
                continue

            post = posts_state[pid]
            for rid in by_post.get(pid, []):      # keep post context current (backfills old rows)
                records[rid]["post_created_at"] = post.get("created_at", "")
                records[rid]["post_text"] = post.get("text", "")
                records[rid]["post_permalink"] = post.get("permalink", "")
            seen = set()
            for r in replies:
                rid = r["id"]
                seen.add(rid)
                payload = json.dumps(r, sort_keys=True, ensure_ascii=False)
                digest = sha256(payload)
                rec = records.get(rid)
                if rec and digest == rec.get("last_sha256"):
                    rec["last_seen_at"] = run_at
                    if rec.get("missing_since"):
                        rec["missing_since"] = ""
                        record_event(state, run_at, rid, pid, "reappeared", "")
                        run["reappeared"] += 1
                    save_state(state)
                    continue
                # new or changed -> write raw evidence line
                raw.write(json.dumps({"captured_at": run_at, "post_id": pid,
                                      "sha256": digest, "data": r},
                                     ensure_ascii=False) + "\n")
                if rec is None:
                    text_present = "text" in r
                    records[rid] = {
                        "status": "live", "username": r.get("username", ""),
                        "text": r.get("text", "") if text_present else "",
                        "original_text": r.get("text", "") if text_present else "",
                        "reply_created_at": sane_timestamp(r.get("timestamp", "")),
                        "reply_permalink": r.get("permalink", ""),
                        "post_id": pid, "post_text": post.get("text", ""),
                        "post_created_at": post.get("created_at", ""),
                        "post_permalink": post.get("permalink", ""),
                        "replied_to_id": (r.get("replied_to") or {}).get("id", ""),
                        "hide_status": r.get("hide_status", ""),
                        "first_captured_at": run_at, "last_seen_at": run_at,
                        "missing_since": "", "edited": "",
                        "sha256_first_capture": digest, "last_sha256": digest,
                        "media": [],
                    }
                    rec = records[rid]
                    record_event(state, run_at, rid, pid, "captured", digest)
                    by_post.setdefault(pid, []).append(rid)
                    run["new"] += 1
                else:
                    rec["last_seen_at"] = run_at
                    rec["last_sha256"] = digest
                    rec["hide_status"] = r.get("hide_status", rec.get("hide_status", ""))
                    if rec.get("missing_since"):
                        rec["missing_since"] = ""
                        record_event(state, run_at, rid, pid, "reappeared", "")
                        run["reappeared"] += 1
                    # backfill fields that were empty under a degraded field set
                    new_parent = (r.get("replied_to") or {}).get("id", "")
                    if not rec.get("username") and r.get("username"):
                        rec["username"] = r["username"]
                    if not rec.get("reply_permalink") and r.get("permalink"):
                        rec["reply_permalink"] = r["permalink"]
                    if not rec.get("replied_to_id") and new_parent:
                        rec["replied_to_id"] = new_parent
                    # field presence vs edit-to-empty: a missing text field is
                    # not a confirmed edit to an empty string
                    if "text" in r and r["text"] != rec["text"]:
                        rec["text"] = r["text"]
                        rec["edited"] = "yes"
                        record_event(state, run_at, rid, pid, "edited",
                                     f"-> {r['text'][:80]}")
                        run["edited"] += 1
                # media evidence: download attachments for new/changed media
                media_urls = collect_media_urls(r)
                for u in media_urls:
                    entry = next((m for m in rec["media"] if m.get("url") == u), None)
                    if entry and entry.get("sha256"):
                        continue                    # already have the bytes
                    path, dgst, err = download_media(u, media_budget)
                    if entry is None:
                        entry = {"url": u}
                        rec["media"].append(entry)
                    entry.update(path=path or "", sha256=dgst or "", error=err or "")
                    record_event(state, run_at, rid, pid,
                                 "media_downloaded" if dgst else "media_download_failed",
                                 dgst or (err or "")[:200])
            # mark missing ONLY from a completed scan
            if complete:
                for rid in by_post.get(pid, []):
                    rec = records[rid]
                    if rid not in seen and not rec.get("missing_since"):
                        rec["missing_since"] = run_at
                        record_event(state, run_at, rid, pid, "disappeared", "")
                        run["newly_missing"] += 1
            else:
                log(f"Post {pid}: scan incomplete ({note}) — nothing marked missing.")
            post["check_status"] = "ok"
            post["check_error"] = ""
            post["last_checked_at"] = run_at
            run["posts_completed"] += 1
            save_state(state)                     # per-post commit: crash-safe
            if i % 50 == 0:
                print(f"  ... {i}/{len(ordered)} posts")
    finally:
        raw.close()

    run["ended_at"] = now_iso()
    state["last_run"] = run
    save_state(state)
    rows = write_csv(records)
    log(f"@{state['username']}: {run['posts_attempted']} posts attempted, "
        f"{run['posts_completed']} completed, {run['posts_failed']} failed, "
        f"{run['posts_inaccessible']} inaccessible; {run['new']} new, "
        f"{run['edited']} edited, {run['newly_missing']} newly missing, "
        f"{run['reappeared']} reappeared; {len(records)} total archived, "
        f"{rows} CSV rows.")


def main():
    DATA.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Previous run still going; skipping.")
        return
    try:
        run()
    except ApiError as e:
        if e.graph_code == 190:
            die("Token expired or revoked. Generate a new one and paste it into config.env.")
        die(e)


if __name__ == "__main__":
    main()
