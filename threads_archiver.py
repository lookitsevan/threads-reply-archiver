#!/usr/bin/env python3
"""
Threads Reply Archiver v1.1.0
-----------------------------
Append-only archive of replies on YOUR OWN Threads posts, meant to run every 15 min.

- Identity comes only from the token in config.env (no handles in code).
- data/state.json is the source of truth; data/archive.csv is built from it.
- Your `tag` and `notes` columns in archive.csv are kept across updates.
- Replies that disappear are kept and marked missing (never deleted).
- Every new or changed reply is written untouched to data/raw/*.jsonl with a SHA-256 hash.
- History (captured / edited / disappeared / reappeared) goes to data/events.jsonl.

v1.1.0 was reviewed and hardened with help from @sorcerai on GitHub (pull request #1):
- pagination tracks completeness; a reply is only marked missing after a fully
  completed scan (empty pages are followed, cursor loops are detected)
- every archived post keeps getting rechecked, including posts older than the
  lookback window (those are rechecked once a day)
- progress is saved during the run, so a crash keeps earlier work
- network problems of every kind become one clean error instead of a crash
- one unexpected problem on one post never blocks the other posts
- if Meta refuses a field, only that field is dropped (or the basic set is used)
- dates are stored exactly as Meta sends them and never erased
- older save files (v1.0.x) are upgraded automatically on first run
- media is noted (has_media / media_type) but never downloaded
- light on disk: saves on a timer and only when something changed

Standard library only (no installs needed). Works with the Python that ships with macOS (3.9+).
"""
import csv, fcntl, hashlib, json, os, re, shutil, socket, sys, time, traceback
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.1.0"

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
STATE_PATH = DATA / "state.json"
EVENTS_PATH = DATA / "events.jsonl"
CSV_PATH = DATA / "archive.csv"
CSV_BAK = DATA / "archive.csv.bak"
LOG_PATH = DATA / "log.txt"
LOCK_PATH = DATA / ".lock"

SCHEMA = 2
POST_FIELDS = "id,text,timestamp,permalink,media_type,username"
# Reply fields. If Meta refuses one, only that one is dropped (remembered in
# state.json and retried once a day). The basic set is never dropped.
REPLY_FIELDS_BASE = [
    "id", "text", "username", "permalink", "timestamp", "media_type",
    "has_replies", "is_reply", "replied_to", "root_post", "hide_status",
]
REPLY_FIELDS_MIN = ["id", "text", "username", "permalink", "timestamp"]

# hide_status values: only these mean the reply is suppressed from public view.
HIDE_STATUS_HIDDEN = {"HIDDEN", "COVERED", "BLOCKED", "RESTRICTED"}
# media_type values that mean "no attachment"
MEDIA_NONE = {"TEXT_POST", "REPOST_FACADE"}

RUN_DEADLINE_S = 20 * 60      # stop scanning after this; finalize the partial run
OLD_POST_RECHECK_S = 24 * 3600  # posts outside the lookback window: recheck once a day
CHECKPOINT_S = 60             # save progress at most once a minute during a run
CSV_REFRESH_S = 3600          # rebuild the spreadsheet at least hourly (or on any change)
FUTURE_SKEW_S = 24 * 3600     # dates this far in the future get a warning (Mac clock?)

CSV_COLUMNS = [
    # --- which post (group by this) ---
    "post_label", "post_created_at", "post_text", "post_permalink",
    # --- the reply ---
    "reply_created_at", "username", "text", "has_media", "media_type",
    "reply_type", "status",
    # --- yours to fill in ---
    "tag", "notes",
    # --- history / evidence ---
    "edited", "original_text", "reply_permalink", "hide_status",
    "first_captured_at", "last_seen_at", "missing_since",
    "post_id", "reply_id", "replied_to_id", "sha256_first_capture",
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


def parse_ts(ts):
    """datetime from Meta ('...+0000') or our own ('...+00:00') format, else None.
    Python 3.9 (macOS) can't read '+0000', so the colon is added first."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def keep_timestamp(ts):
    """Return Meta's date text exactly as sent. Never erase evidence:
    odd dates only produce a warning in the log."""
    if not ts:
        return ""
    dt = parse_ts(ts)
    if dt is None:
        log(f"WARNING: unreadable date from Meta kept as-is: {ts}")
    elif (dt - utcnow()).total_seconds() > FUTURE_SKEW_S:
        log(f"WARNING: date in the future kept as-is (is this Mac's clock right?): {ts}")
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
    log(f"ERROR: {msg}")
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
    req = urllib.request.Request(url, headers={"User-Agent": f"threads-reply-archiver/{VERSION}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()
                return json.loads(body.decode("utf-8"))
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
            err = ApiError(redact(f"network problem: {type(e).__name__}: {e}"))
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise err


def paginate(path, params, max_pages=100):
    """Follow paging cursors and report whether the scan was complete.

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
            return items, False, "too many empty pages in a row"
        url = nxt
    return items, False, f"hit the {max_pages}-page limit"


_FIELD_RE = re.compile(
    r"(?:nonexisting field|unknown field|invalid field)\s*\(?['\"]?"
    r"([a-zA-Z_][a-zA-Z0-9_]*)['\"]?\)?",
    re.IGNORECASE)


def is_missing_object(e):
    """The post itself is gone or off-limits (not a field problem)."""
    return e.http_code == 404 or (e.graph_code == 100 and e.graph_subcode in (33, 210))


def is_field_error(e):
    return (not is_missing_object(e) and "field" in str(e).lower()
            and (e.graph_code == 100 or e.http_code == 400))


def current_reply_fields(state):
    dropped = set(state.get("fields_dropped", []))
    return ",".join(f for f in REPLY_FIELDS_BASE if f not in dropped)


def fetch_conversation(post_id, state):
    """(items, complete, note). If Meta refuses a field, drop just that field;
    if it can't be identified, fall back to the basic field set (never fail forever)."""
    for _ in range(len(REPLY_FIELDS_BASE)):
        fields = current_reply_fields(state)
        try:
            return paginate(f"{post_id}/conversation", {"fields": fields, "reverse": "false"})
        except ApiError as e:
            if not is_field_error(e):
                raise
            dropped = state["fields_dropped"]
            m = _FIELD_RE.search(str(e))
            field = m.group(1) if m else None
            if field in REPLY_FIELDS_BASE and field not in REPLY_FIELDS_MIN and field not in dropped:
                dropped.append(field)
                log(f"Meta refused the field '{field}'. Dropping just that one "
                    f"(retried once a day).")
            else:
                extras = [f for f in REPLY_FIELDS_BASE
                          if f not in REPLY_FIELDS_MIN and f not in dropped]
                if not extras:
                    raise
                dropped.extend(extras)
                log("Meta refused a field it didn't name. Using the basic field set "
                    "for now (retried once a day).")
            save_state(state)
    raise ApiError("could not find a field set Meta accepts")


# ---------- state, saving, history ----------
_dirty = False
_last_save = 0.0
_saves = 0
_events_buf = []


def mark_dirty():
    global _dirty
    _dirty = True


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    """Write history first, then state (atomic replace)."""
    global _dirty, _last_save, _saves
    flush_events()
    tmp = DATA / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)
    _dirty, _last_save, _saves = False, time.monotonic(), _saves + 1


def checkpoint(state, force=False):
    """Save only if something changed, and at most once a minute unless forced."""
    if _dirty and (force or time.monotonic() - _last_save >= CHECKPOINT_S):
        save_state(state)


def record_event(at, reply_id, post_id, etype, detail=""):
    _events_buf.append({"at": at, "reply_id": reply_id, "post_id": post_id,
                        "type": etype, "detail": detail})
    mark_dirty()


def flush_events():
    """Append-only history file: only new lines are ever written."""
    global _events_buf
    if not _events_buf:
        return
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        for ev in _events_buf:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    _events_buf = []


def migrate(state):
    """Bring any older save file (v1.0.x) up to date before anything else runs."""
    state.setdefault("replies", {})
    state.setdefault("posts", {})
    state.setdefault("fields_dropped", [])
    for ev in state.pop("events", []) or []:       # pre-release builds kept history here
        _events_buf.append(ev)
    defaults = {"status": "live", "username": "", "text": "", "reply_created_at": "",
                "reply_permalink": "", "post_id": "", "post_text": "", "post_created_at": "",
                "post_permalink": "", "replied_to_id": "", "hide_status": "",
                "media_type": "", "first_captured_at": "", "last_seen_at": "",
                "missing_since": "", "edited": "", "sha256_first_capture": "",
                "last_sha256": ""}
    for rid, rec in state["replies"].items():
        for k, v in defaults.items():
            if k not in rec:
                rec[k] = v
        if "original_text" not in rec:
            rec["original_text"] = rec.get("text", "")
        rec.pop("media", None)                     # pre-release builds only
        # older posts keep getting rechecked: seed the post list from saved replies
        pid = rec.get("post_id")
        if pid and pid not in state["posts"]:
            state["posts"][pid] = {
                "text": rec.get("post_text", ""), "created_at": rec.get("post_created_at", ""),
                "permalink": rec.get("post_permalink", ""), "media_type": "", "username": "",
                "first_seen_at": rec.get("first_captured_at", ""), "check_status": "ok",
                "check_error": "", "last_checked_at": "", "in_window": False,
            }
    if state.get("schema") != SCHEMA:
        if state.get("replies"):
            log(f"Upgraded your saved archive to the v{VERSION} format "
                f"({len(state['replies'])} replies kept).")
        state["schema"] = SCHEMA
        mark_dirty()


# ---------- spreadsheet ----------
def status_of(rec):
    """'missing' means gone from Meta's API, not proven deleted."""
    if rec.get("missing_since"):
        return "missing"
    if (rec.get("hide_status") or "") in HIDE_STATUS_HIDDEN:
        return "live (hidden)"
    return "live"


def has_media(media_type):
    if not media_type:
        return ""                                  # unknown (field unavailable)
    return "no" if media_type in MEDIA_NONE else "yes"


def csv_safe(value):
    """Block spreadsheet formula tricks, including ones hidden behind spaces or line breaks."""
    s = "" if value is None else str(value)
    return "'" + s if s.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@") else s


def read_csv_annotations(records):
    kept, header = {}, []
    if not CSV_PATH.exists():
        return kept, header
    try:
        with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            if {"reply_id", "tag", "notes"} <= set(header):
                for row in reader:
                    rid = (row.get("reply_id") or "").strip()
                    if rid and rid in records:
                        kept[rid] = {k: row.get(k, "") or "" for k in USER_COLUMNS}
    except (OSError, csv.Error) as e:
        log(f"WARNING: could not read your tags/notes from archive.csv: {e}")
    return kept, header


def write_csv(records, owner):
    kept, _ = read_csv_annotations(records)
    rows = []
    for rid, rec in records.items():
        row = {c: rec.get(c, "") for c in CSV_COLUMNS}
        row["reply_id"] = rid
        row["status"] = status_of(rec)
        row["has_media"] = has_media(rec.get("media_type", ""))
        snippet = " ".join((rec.get("post_text") or "(no text)").split())[:50]
        row["post_label"] = f"{(rec.get('post_created_at') or '')[:10]} · {snippet}"
        parent = rec.get("replied_to_id", "")
        if not parent or parent == rec.get("post_id"):
            row["reply_type"] = "reply to post"
        else:
            who = records.get(parent, {}).get("username", "")
            row["reply_type"] = f"↳ reply to @{who}" if who else "↳ nested reply"
        if not row["hide_status"] and owner and rec.get("username") == owner:
            row["hide_status"] = "(your reply)"
        for c in TEXT_COLUMNS:
            row[c] = csv_safe(row[c])
        row.update(kept.get(rid, {}))
        rows.append(row)
    # replies oldest-first inside each post, posts newest-first (stable two-pass sort)
    rows.sort(key=lambda r: r["reply_created_at"])
    rows.sort(key=lambda r: (r["post_created_at"], r["post_id"]), reverse=True)
    if CSV_PATH.exists():
        try:
            shutil.copy2(CSV_PATH, CSV_BAK)
        except OSError as e:
            log(f"WARNING: could not back up archive.csv: {e}")
    tmp = DATA / "archive.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:   # -sig so Excel shows emoji
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CSV_PATH)
    return len(rows)


def csv_needs_rebuild(state, changed):
    if changed or not CSV_PATH.exists():
        return True
    if sys.stdout.isatty():                        # you opened it yourself: show fresh times
        return True
    try:
        with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
            if next(csv.reader(f), []) != CSV_COLUMNS:
                return True                        # older layout: upgrade the columns
    except (OSError, csv.Error, StopIteration):
        return True
    last = parse_ts(state.get("csv_written_at", ""))
    return last is None or (utcnow() - last).total_seconds() >= CSV_REFRESH_S


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------- token upkeep ----------
def maybe_refresh_token(state):
    """Long-lived tokens last 60 days; refresh weekly so it never lapses."""
    global TOKEN
    ok = parse_ts(state.get("token_refreshed_at", ""))
    if ok and ok > utcnow() - timedelta(days=7):
        return
    tried = parse_ts(state.get("token_refresh_attempted_at", ""))
    if tried and tried > utcnow() - timedelta(days=1):
        return
    state["token_refresh_attempted_at"] = now_iso()
    mark_dirty()
    try:
        data = api_get(REFRESH_URL, {"grant_type": "th_refresh_token", "access_token": TOKEN})
    except ApiError as e:
        log(f"Token refresh skipped (normal if the token is under 24 hours old): {e}")
        return
    new = data.get("access_token")
    if not new:
        return
    try:
        save_env_value("THREADS_ACCESS_TOKEN", new)
    except OSError as e:
        die(f"Meta renewed your token but config.env could not be saved: {e}. "
            f"Your old token may still work; fix the disk problem and run again.")
    TOKEN = new
    state["token_refreshed_at"] = now_iso()
    save_state(state)                              # persist right away
    log("Access token refreshed.")


# ---------- main scan ----------
def run():
    global TOKEN
    env = load_env()
    TOKEN = env.get("THREADS_ACCESS_TOKEN", "")
    if not TOKEN or TOKEN.startswith("paste"):
        die("No token in config.env yet. Paste it after THREADS_ACCESS_TOKEN= and save.")
    try:
        lookback = int(env.get("LOOKBACK_DAYS", "30") or 30)
    except ValueError:
        lookback = 30
        log("WARNING: LOOKBACK_DAYS in config.env isn't a number; using 30.")

    state = load_state()
    migrate(state)
    if state.get("run_open"):
        log("The last run didn't finish cleanly; anything it saved is kept.")
    state["run_open"] = True
    save_state(state)
    try:
        _scan(state, lookback)
    finally:
        if state.get("run_open"):                   # the scan stopped early: keep what we have
            state["run_open"] = False
            try:
                save_state(state)
            except OSError as e:
                log(f"WARNING: could not save final progress: {e}")


def _scan(state, lookback):
    print(f"Threads Reply Archiver v{VERSION}")
    print("Contacting Threads...")
    maybe_refresh_token(state)

    # identity gate: stop early if the token itself is the problem
    try:
        me = api_get(f"{BASE}/me", {"fields": "id,username", "access_token": TOKEN})
    except ApiError as e:
        if e.http_code in (400, 401, 403) or e.graph_code == 190:
            die(f"Threads rejected your token ({e}). Make a new token and paste "
                f"it into config.env.")
        raise
    if state.get("account_id") and state["account_id"] != me["id"]:
        die("This archive belongs to a different Threads account. Use a fresh copy of the folder.")
    if state.get("account_id") != me["id"] or state.get("username") != me.get("username", ""):
        state["account_id"], state["username"] = me["id"], me.get("username", "")
        mark_dirty()
    owner = state["username"]

    # retry refused fields once a day; if Meta accepts them again, they're back
    dropped = state.get("fields_dropped", [])
    last_try = parse_ts(state.get("fields_retry_at", ""))
    if dropped and (last_try is None or utcnow() - last_try > timedelta(days=1)):
        state["fields_dropped"] = []
        state["fields_retry_at"] = now_iso()
        mark_dirty()
        log("Trying the full set of reply fields again.")

    deadline = time.monotonic() + RUN_DEADLINE_S
    run_at = now_iso()
    stats = {"posts_checked": 0, "posts_failed": 0, "posts_inaccessible": 0,
             "posts_waiting": 0, "new": 0, "edited": 0, "newly_missing": 0, "reappeared": 0}

    # 1. DISCOVERY: the lookback window finds posts to watch closely (every run)
    since = int((utcnow() - timedelta(days=lookback)).timestamp())
    print(f"Signed in as @{owner}. Looking for posts from the last {lookback} days...")
    posts, disc_complete, disc_note = paginate("me/threads", {"fields": POST_FIELDS, "since": since})
    if not disc_complete:
        log(f"WARNING: couldn't list every recent post this run ({disc_note}).")
    posts_state = state["posts"]
    window_ids = set()
    for p in posts:
        pid = p["id"]
        window_ids.add(pid)
        rec = posts_state.get(pid, {})
        new_info = {"text": p.get("text", ""), "created_at": keep_timestamp(p.get("timestamp", "")),
                    "permalink": p.get("permalink", ""), "media_type": p.get("media_type", ""),
                    "username": p.get("username", "")}
        if any(rec.get(k) != v for k, v in new_info.items()) or not rec:
            rec.update(new_info)
            rec.setdefault("first_seen_at", run_at)
            rec.setdefault("check_status", "ok")
            rec.setdefault("check_error", "")
            rec.setdefault("last_checked_at", "")
            posts_state[pid] = rec
            mark_dirty()
    for pid, rec in posts_state.items():
        in_window = pid in window_ids
        if rec.get("in_window") != in_window:
            rec["in_window"] = in_window
            mark_dirty()

    # 2. RECHECK: recent posts every run; older ones once a day. Oldest-checked first.
    def due(pid):
        rec = posts_state[pid]
        if rec.get("in_window"):
            return True
        last = parse_ts(rec.get("last_checked_at", ""))
        return last is None or (utcnow() - last).total_seconds() >= OLD_POST_RECHECK_S
    ordered = sorted(posts_state, key=lambda pid: posts_state[pid].get("last_checked_at") or "")
    todo = [pid for pid in ordered if due(pid)]
    stats["posts_waiting"] = len(ordered) - len(todo)
    print(f"Watching {len(posts_state)} posts. Checking {len(todo)} now...")

    records = state["replies"]
    by_post = {}
    for rid, rec in records.items():
        by_post.setdefault(rec.get("post_id", ""), []).append(rid)

    raw = (RAW / f"{run_at[:10]}.jsonl").open("a", encoding="utf-8")
    try:
        for i, pid in enumerate(todo, 1):
            if time.monotonic() > deadline:
                log("Time limit reached; saving and stopping. The next run picks up the rest.")
                break
            post = posts_state[pid]
            try:
                changed = process_post(pid, post, state, records, by_post, raw, run_at, stats)
            except ApiError as e:
                bucket = "inaccessible" if is_missing_object(e) else "failed"
                post.update(check_status=bucket, check_error=str(e)[:300], last_checked_at=run_at)
                stats["posts_" + bucket] += 1
                if bucket == "inaccessible":
                    record_event(run_at, "", pid, "post_inaccessible", str(e)[:300])
                    log(f"Post {pid}: can't be reached anymore; its replies keep their last status.")
                else:
                    log(f"Post {pid}: skipped this run, nothing marked missing ({e}).")
                mark_dirty()
            except Exception as e:                  # safety net: one post never blocks the rest
                post.update(check_status="failed", check_error=f"{type(e).__name__}: {e}"[:300],
                            last_checked_at=run_at)
                stats["posts_failed"] += 1
                mark_dirty()
                log(f"Post {pid}: unexpected problem, skipped this run ({type(e).__name__}: {e}).")
            checkpoint(state)                        # at most once a minute, only if changed
            if i % 25 == 0:
                print(f"  ... {i}/{len(todo)} posts")
    finally:
        raw.close()

    changed_any = any(stats[k] for k in ("new", "edited", "newly_missing", "reappeared"))
    state["last_run"] = dict(stats, started_at=run_at, ended_at=now_iso(), version=VERSION)
    mark_dirty()
    stale = state.pop("csv_stale", False)
    if csv_needs_rebuild(state, changed_any or stale):
        rows = write_csv(records, owner)
        state["csv_written_at"] = now_iso()
        csv_note = f"spreadsheet updated ({rows} rows)"
    else:
        csv_note = "spreadsheet unchanged"
    state["run_open"] = False
    checkpoint(state, force=True)
    log(f"@{owner}: {stats['posts_checked']} posts checked, {stats['posts_failed']} failed, "
        f"{stats['posts_inaccessible']} unreachable, {stats['posts_waiting']} older posts "
        f"waiting for their daily check; {stats['new']} new, {stats['edited']} edited, "
        f"{stats['newly_missing']} newly missing, {stats['reappeared']} reappeared; "
        f"{len(records)} total archived; {csv_note}.")


def process_post(pid, post, state, records, by_post, raw, run_at, stats):
    replies, complete, note = fetch_conversation(pid, state)
    for rid in by_post.get(pid, []):               # keep post context current
        rec = records[rid]
        for rk, pk in (("post_created_at", "created_at"), ("post_text", "text"),
                       ("post_permalink", "permalink")):
            if post.get(pk) and rec.get(rk) != post.get(pk):
                rec[rk] = post[pk]
                state["csv_stale"] = True
                mark_dirty()
    seen = set()
    for r in replies:
        rid = r["id"]
        seen.add(rid)
        payload = json.dumps(r, sort_keys=True, ensure_ascii=False)
        digest = sha256(payload)
        rec = records.get(rid)
        if rec is None:
            raw.write(json.dumps({"captured_at": run_at, "post_id": pid,
                                  "sha256": digest, "data": r}, ensure_ascii=False) + "\n")
            text = r.get("text", "") if "text" in r else ""
            records[rid] = {
                "status": "live", "username": r.get("username", ""),
                "text": text, "original_text": text,
                "reply_created_at": keep_timestamp(r.get("timestamp", "")),
                "reply_permalink": r.get("permalink", ""),
                "media_type": r.get("media_type", ""),
                "post_id": pid, "post_text": post.get("text", ""),
                "post_created_at": post.get("created_at", ""),
                "post_permalink": post.get("permalink", ""),
                "replied_to_id": (r.get("replied_to") or {}).get("id", ""),
                "hide_status": r.get("hide_status", ""),
                "first_captured_at": run_at, "last_seen_at": run_at,
                "missing_since": "", "edited": "",
                "sha256_first_capture": digest, "last_sha256": digest,
            }
            by_post.setdefault(pid, []).append(rid)
            record_event(run_at, rid, pid, "captured", digest)
            stats["new"] += 1
            continue

        rec["last_seen_at"] = run_at
        mark_dirty()
        if rec.get("missing_since"):
            rec["missing_since"] = ""
            record_event(run_at, rid, pid, "reappeared")
            stats["reappeared"] += 1
        # fill in anything an older version or a reduced field set left empty
        for rk, value in (("username", r.get("username")), ("reply_permalink", r.get("permalink")),
                          ("media_type", r.get("media_type")),
                          ("replied_to_id", (r.get("replied_to") or {}).get("id"))):
            if value and not rec.get(rk):
                rec[rk] = value
                state["csv_stale"] = True
        if digest == rec.get("last_sha256"):
            continue
        # changed since last time -> keep an untouched copy as evidence
        raw.write(json.dumps({"captured_at": run_at, "post_id": pid,
                              "sha256": digest, "data": r}, ensure_ascii=False) + "\n")
        rec["last_sha256"] = digest
        if "hide_status" in r and r["hide_status"] != rec.get("hide_status"):
            rec["hide_status"] = r["hide_status"]
            state["csv_stale"] = True
        if "media_type" in r and r["media_type"] != rec.get("media_type"):
            rec["media_type"] = r["media_type"]
            state["csv_stale"] = True
        # a missing text field is NOT an edit to empty text
        if "text" in r and r["text"] != rec.get("text", ""):
            rec["text"] = r["text"]
            rec["edited"] = "yes"
            record_event(run_at, rid, pid, "edited", r["text"][:80])
            stats["edited"] += 1

    if complete:                                    # mark missing ONLY after a complete scan
        for rid in by_post.get(pid, []):
            rec = records[rid]
            if rid not in seen and not rec.get("missing_since"):
                rec["missing_since"] = run_at
                record_event(run_at, rid, pid, "disappeared")
                stats["newly_missing"] += 1
    else:
        log(f"Post {pid}: couldn't read every reply ({note}); nothing marked missing.")
    post.update(check_status="ok", check_error="", last_checked_at=run_at)
    stats["posts_checked"] += 1
    mark_dirty()
    return True


def main():
    DATA.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
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
            die("Your token expired or was removed. Make a new one and paste it into config.env.")
        die(e)
    except SystemExit:
        raise
    except Exception:
        die("Unexpected problem:\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
