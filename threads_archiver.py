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

Standard library only (no installs needed).
"""
import csv, fcntl, hashlib, json, os, socket, sys, time
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
STATE_PATH = DATA / "state.json"
CSV_PATH = DATA / "archive.csv"
LOG_PATH = DATA / "log.txt"
LOCK_PATH = DATA / ".lock"

POST_FIELDS = "id,text,timestamp,permalink,media_type,username"
REPLY_FIELDS_FULL = ("id,text,username,permalink,timestamp,media_type,"
                     "has_replies,is_reply,replied_to,root_post,hide_status")
REPLY_FIELDS_MIN = "id,text,username,permalink,timestamp"

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
]
USER_COLUMNS = ("tag", "notes")                # columns YOU edit; never overwritten
TEXT_COLUMNS = ("post_label", "username", "text", "post_text", "original_text")

TOKEN = ""
reply_fields = REPLY_FIELDS_FULL


class ApiError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def now_iso():
    return utcnow().replace(microsecond=0).isoformat()


def redact(s):
    return s.replace(TOKEN, "***TOKEN***") if TOKEN else s


def log(msg):
    line = f"{now_iso()}  {redact(str(msg))}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


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
def api_get(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "threads-reply-archiver/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(f"HTTP {e.code}: {redact(body)}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(f"Network error: {e.reason}")


def paginate(path, params, max_pages=100):
    url = f"{BASE}/{path}"
    q = dict(params, access_token=TOKEN)
    items = []
    for _ in range(max_pages):
        data = api_get(url, q)
        q = None                                  # 'next' URLs already carry params
        batch = data.get("data", [])
        if not batch:                             # empty page = done (Meta can send endless 'next' links)
            break
        items.extend(batch)
        url = data.get("paging", {}).get("next")
        if not url:
            break
    return items


def fetch_conversation(post_id):
    """All replies at any depth under one post. Falls back to minimal fields if needed."""
    global reply_fields
    try:
        return paginate(f"{post_id}/conversation", {"fields": reply_fields, "reverse": "false"})
    except ApiError as e:
        if reply_fields == REPLY_FIELDS_FULL and ("(#100)" in str(e) or "field" in str(e).lower()):
            log("Some reply fields unsupported; switching to minimal field set.")
            reply_fields = REPLY_FIELDS_MIN
            return paginate(f"{post_id}/conversation", {"fields": reply_fields, "reverse": "false"})
        raise


# ---------- token upkeep ----------
def maybe_refresh_token(state):
    """Long-lived tokens last 60 days; refresh weekly so it never lapses."""
    global TOKEN
    ok = state.get("token_refreshed_at")
    tried = state.get("token_refresh_attempted_at")
    if ok and datetime.fromisoformat(ok) > utcnow() - timedelta(days=7):
        return
    if tried and datetime.fromisoformat(tried) > utcnow() - timedelta(days=1):
        return
    state["token_refresh_attempted_at"] = now_iso()
    try:
        data = api_get(REFRESH_URL, {"grant_type": "th_refresh_token", "access_token": TOKEN})
        new = data.get("access_token")
        if new:
            TOKEN = new
            save_env_value("THREADS_ACCESS_TOKEN", new)
            state["token_refreshed_at"] = now_iso()
            save_state(state)                     # persist now so a later crash doesn't re-refresh
            log("Access token refreshed.")
    except ApiError as e:
        log(f"Token refresh skipped (normal if token is under 24h old): {e}")


# ---------- storage ----------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"replies": {}}


def save_state(state):
    tmp = DATA / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def csv_safe(value):
    """Block spreadsheet formula injection (a comment like =HYPERLINK(...) would execute)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def write_csv(records):
    kept = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                kept[row.get("reply_id", "")] = {k: row.get(k, "") or "" for k in USER_COLUMNS}
    rows = []
    for rid, rec in records.items():
        row = {c: rec.get(c, "") for c in CSV_COLUMNS}
        row["reply_id"] = rid
        snippet = " ".join((rec.get("post_text") or "(no text)").split())[:50]
        row["post_label"] = f"{(rec.get('post_created_at') or '')[:10]} · {snippet}"
        parent = rec.get("replied_to_id", "")
        if not parent or parent == rec.get("post_id"):
            row["reply_type"] = "reply to post"
        else:
            who = records.get(parent, {}).get("username", "")
            row["reply_type"] = f"↳ reply to @{who}" if who else "↳ nested reply"
        for c in TEXT_COLUMNS:
            row[c] = csv_safe(row[c])
        row.update(kept.get(rid, {}))
        rows.append(row)
    # replies oldest-first inside each post, posts newest-first (stable two-pass sort)
    rows.sort(key=lambda r: r["reply_created_at"])
    rows.sort(key=lambda r: (r["post_created_at"], r["post_id"]), reverse=True)
    tmp = DATA / "archive.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:   # -sig so Excel shows emoji
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CSV_PATH)


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------- main ----------
def run():
    global TOKEN
    env = load_env()
    TOKEN = env.get("THREADS_ACCESS_TOKEN", "")
    if not TOKEN or TOKEN.startswith("paste"):
        die("No token in config.env yet. Paste it after THREADS_ACCESS_TOKEN= and save.")
    lookback = int(env.get("LOOKBACK_DAYS", "30") or 30)

    state = load_state()
    print("Contacting Threads...")
    maybe_refresh_token(state)

    me = api_get(f"{BASE}/me", {"fields": "id,username", "access_token": TOKEN})
    if state.get("account_id") and state["account_id"] != me["id"]:
        die("This archive belongs to a different Threads account. Use a fresh copy of the folder.")
    state["account_id"], state["username"] = me["id"], me.get("username", "")

    since = int((utcnow() - timedelta(days=lookback)).timestamp())
    print(f"Signed in as @{me.get('username','?')}. Fetching posts from the last {lookback} days...")
    posts = paginate("me/threads", {"fields": POST_FIELDS, "since": since})
    print(f"Found {len(posts)} posts. Checking replies...")

    records = state.setdefault("replies", {})
    by_post = {}
    for rid, rec in records.items():
        by_post.setdefault(rec["post_id"], []).append(rid)

    run_at = now_iso()
    new = edited = missing = 0
    with (RAW / f"{run_at[:10]}.jsonl").open("a", encoding="utf-8") as raw:
        for i, post in enumerate(posts, 1):
            pid = post["id"]
            try:
                replies = fetch_conversation(pid)
                print(f"  post {i}/{len(posts)}: {len(replies)} replies")
            except ApiError as e:
                log(f"Skipped post {pid} this run (nothing marked missing): {e}")
                continue
            for rid in by_post.get(pid, []):      # keep post context current (backfills old rows)
                records[rid]["post_created_at"] = post.get("timestamp", "")
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
                    if rec["status"] == "missing":
                        rec["status"], rec["missing_since"] = "live", ""
                    continue
                # new or changed -> write raw evidence line
                raw.write(json.dumps({"captured_at": run_at, "post_id": pid,
                                      "sha256": digest, "data": r}, ensure_ascii=False) + "\n")
                if rec is None:
                    records[rid] = {
                        "status": "live", "username": r.get("username", ""),
                        "text": r.get("text", ""), "original_text": r.get("text", ""),
                        "reply_created_at": r.get("timestamp", ""),
                        "reply_permalink": r.get("permalink", ""),
                        "post_id": pid, "post_text": post.get("text", ""),
                        "post_created_at": post.get("timestamp", ""),
                        "post_permalink": post.get("permalink", ""),
                        "replied_to_id": (r.get("replied_to") or {}).get("id", ""),
                        "hide_status": r.get("hide_status", ""),
                        "first_captured_at": run_at, "last_seen_at": run_at,
                        "missing_since": "", "edited": "",
                        "sha256_first_capture": digest, "last_sha256": digest,
                    }
                    by_post.setdefault(pid, []).append(rid)
                    new += 1
                else:
                    rec.update(last_seen_at=run_at, last_sha256=digest,
                               hide_status=r.get("hide_status", rec.get("hide_status", "")))
                    if rec["status"] == "missing":
                        rec["status"], rec["missing_since"] = "live", ""
                    if r.get("text", "") != rec["text"]:
                        rec["text"], rec["edited"] = r.get("text", ""), "yes"
                        edited += 1
            for rid in by_post.get(pid, []):
                rec = records[rid]
                if rid not in seen and rec["status"] == "live":
                    rec["status"], rec["missing_since"] = "missing", run_at
                    missing += 1

    save_state(state)
    write_csv(records)
    log(f"@{state['username']}: {len(posts)} posts checked, {new} new, "
        f"{edited} edited, {missing} newly missing, {len(records)} total archived.")


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
        if '"code":190' in str(e) or "code\": 190" in str(e):
            die("Token expired or revoked. Generate a new one and paste it into config.env.")
        die(e)


if __name__ == "__main__":
    main()
