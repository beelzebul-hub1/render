import os
import json
import time
import threading
import hashlib
import base64
import re
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

DATA_FILE     = os.environ.get("DATA_FILE", "data.json")
DAILY_FILE    = os.environ.get("DAILY_FILE", "daily.json")
MAX_HISTORY   = int(os.environ.get("MAX_HISTORY", "500"))
SAVE_INTERVAL = int(os.environ.get("SAVE_INTERVAL", "60"))

STATE_GITHUB_REPO  = os.environ.get("STATE_GITHUB_REPO")
STATE_GITHUB_TOKEN = os.environ.get("STATE_GITHUB_TOKEN")
STATE_GITHUB_FILE  = os.environ.get("STATE_GITHUB_FILE", "dashboard_state.json")

STATE_ENCRYPT_KEY = os.environ.get("STATE_ENCRYPT_KEY") or STATE_GITHUB_TOKEN or "twitch-dashboard-default-key"

def _xor_cipher(data: bytes, key: str) -> bytes:
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    return bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data))

def encrypt_token(plain: str) -> str:
    if not plain:
        return plain
    return "enc:" + base64.b64encode(_xor_cipher(plain.encode("utf-8"), STATE_ENCRYPT_KEY)).decode("ascii")

def decrypt_token(value: str) -> str:
    if not value or not value.startswith("enc:"):
        return value
    try:
        raw = base64.b64decode(value[4:].encode("ascii"))
        return _xor_cipher(raw, STATE_ENCRYPT_KEY).decode("utf-8")
    except Exception as e:
        print("token decrypt error:", e)
        return value

POINTS_CACHE = {}
HISTORY      = {}
DAILY        = {}
PEAK         = {}
STREAMER_LOG = {}
UPTIME       = {}
CRASH_COUNT  = {}
SILENCE_LOG  = {}
NICKNAMES    = {}
PINNED       = set()
EVENT_LOG    = []
MAX_EVENT_LOG = 500
_prev_status = {}
_silence_start = {}
GITHUB_REPOS = {}  # moved here so it exists before load_data/save_data

STATE_BACKUP_STATUS = {
    "last_push_ok": None,
    "last_push_error": None,
    "last_push_time": None,
    "last_pull_ok": None,
    "last_pull_error": None,
    "last_pull_time": None,
}

# ============================================================
# GITHUB HELPERS (needed by load_data/save_data)
# ============================================================

def normalize_repo(raw):
    s = raw.strip()
    s = re.sub(r"^(https?://)?(www\.)?github\.com/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.git$", "", s, flags=re.IGNORECASE)
    s = s.strip("/")
    return s

def gh_get_file(cfg):
    repo  = cfg["repo"]
    token = cfg["token"]
    file  = cfg.get("file", "main.py")
    url   = f"https://api.github.com/repos/{repo}/contents/{file}"
    try:
        r = requests.get(url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }, timeout=10)
    except requests.exceptions.RequestException as e:
        return None, None, f"Network error contacting GitHub: {e}"
    try:
        j = r.json()
    except ValueError:
        return None, None, f"GitHub returned a non-JSON response (status {r.status_code})"
    if r.status_code != 200:
        return None, None, j.get("message", f"GitHub error (status {r.status_code})")
    if isinstance(j, list):
        return None, None, f"'{file}' is a directory, not a file"
    if "content" not in j:
        return None, None, f"Unexpected response from GitHub for '{file}'"
    try:
        content = base64.b64decode(j["content"]).decode("utf-8")
    except Exception as e:
        return None, None, f"Could not decode file content: {e}"
    return content, j["sha"], None

def gh_push_file(cfg, new_content, commit_message="Update bot config from dashboard"):
    repo  = cfg["repo"]
    token = cfg["token"]
    file  = cfg.get("file", "main.py")
    _, sha, err = gh_get_file(cfg)
    _no_file_yet = ("not found", "is empty")
    if err and not any(kw in err.lower() for kw in _no_file_yet):
        return False, err
    url = f"https://api.github.com/repos/{repo}/contents/{file}"
    payload = {
        "message": commit_message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, json=payload, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }, timeout=15)
    except requests.exceptions.RequestException as e:
        return False, f"Network error contacting GitHub: {e}"
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code in (200, 201):
        return True, None
    return False, j.get("message", f"GitHub push failed (status {r.status_code})")

def gh_state_pull():
    if not (STATE_GITHUB_REPO and STATE_GITHUB_TOKEN):
        STATE_BACKUP_STATUS["last_pull_ok"] = False
        STATE_BACKUP_STATUS["last_pull_error"] = "STATE_GITHUB_REPO / STATE_GITHUB_TOKEN not set"
        STATE_BACKUP_STATUS["last_pull_time"] = datetime.now(timezone.utc).isoformat()
        return None
    cfg = {"repo": STATE_GITHUB_REPO, "token": STATE_GITHUB_TOKEN, "file": STATE_GITHUB_FILE}
    content, sha, err = gh_get_file(cfg)
    STATE_BACKUP_STATUS["last_pull_time"] = datetime.now(timezone.utc).isoformat()
    if err:
        STATE_BACKUP_STATUS["last_pull_ok"] = False
        STATE_BACKUP_STATUS["last_pull_error"] = err
        print("state backup pull:", err)
        return None
    try:
        parsed = json.loads(content)
        STATE_BACKUP_STATUS["last_pull_ok"] = True
        STATE_BACKUP_STATUS["last_pull_error"] = None
        return parsed
    except Exception as e:
        STATE_BACKUP_STATUS["last_pull_ok"] = False
        STATE_BACKUP_STATUS["last_pull_error"] = f"parse error: {e}"
        print("state backup parse error:", e)
        return None

def gh_state_push(payload):
    if not (STATE_GITHUB_REPO and STATE_GITHUB_TOKEN):
        STATE_BACKUP_STATUS["last_push_ok"] = False
        STATE_BACKUP_STATUS["last_push_error"] = "STATE_GITHUB_REPO / STATE_GITHUB_TOKEN not set"
        STATE_BACKUP_STATUS["last_push_time"] = datetime.now(timezone.utc).isoformat()
        return
    # Only block if everything is genuinely empty (nothing loaded yet at all)
    is_blank = (
        len(payload.get("points_cache") or {}) == 0 and
        len(payload.get("github_repos") or {}) == 0 and
        len(payload.get("nicknames") or {}) == 0 and
        len(payload.get("pinned") or []) == 0
    )
    if is_blank:
        existing = gh_state_pull()
        if existing and (existing.get("points_cache") or existing.get("github_repos")):
            msg = "skipped: refusing to overwrite a non-empty backup with a blank state"
            STATE_BACKUP_STATUS["last_push_ok"] = False
            STATE_BACKUP_STATUS["last_push_error"] = msg
            STATE_BACKUP_STATUS["last_push_time"] = datetime.now(timezone.utc).isoformat()
            print("state backup push:", msg)
            return
    cfg = {"repo": STATE_GITHUB_REPO, "token": STATE_GITHUB_TOKEN, "file": STATE_GITHUB_FILE}
    ok, err = gh_push_file(cfg, json.dumps(payload), "Auto-backup dashboard state")
    STATE_BACKUP_STATUS["last_push_ok"] = ok
    STATE_BACKUP_STATUS["last_push_error"] = err
    STATE_BACKUP_STATUS["last_push_time"] = datetime.now(timezone.utc).isoformat()
    if not ok:
        print("state backup push:", err)

# ============================================================
# SAVE / LOAD  (single definition each, includes GITHUB_REPOS)
# ============================================================

def save_data():
    payload = {
        "points_cache": POINTS_CACHE,
        "history": {a: list(s) for a,s in HISTORY.items()},
        "peak": PEAK,
        "streamer_log": STREAMER_LOG,
        "uptime": UPTIME,
        "crash_count": CRASH_COUNT,
        "silence_log": SILENCE_LOG,
        "nicknames": NICKNAMES,
        "pinned": list(PINNED),
        "event_log": EVENT_LOG[-MAX_EVENT_LOG:],
        "github_repos": {
            acc: {**cfg, "token": encrypt_token(cfg.get("token", ""))}
            for acc, cfg in GITHUB_REPOS.items()
        },
    }
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print("local save error:", e)
    try:
        gh_state_push(payload)
    except Exception as e:
        print("state backup push error:", e)

def save_daily():
    try:
        with open(DAILY_FILE, "w") as f:
            json.dump(DAILY, f)
    except Exception as e:
        print("daily save error:", e)

def load_data():
    global POINTS_CACHE,HISTORY,PEAK,STREAMER_LOG,UPTIME,CRASH_COUNT,SILENCE_LOG,NICKNAMES,PINNED,GITHUB_REPOS,EVENT_LOG
    p = None
    source = None
    try:
        p = gh_state_pull()
        if p is not None:
            source = "GitHub backup"
    except Exception as e:
        print("state backup pull error:", e)
    if p is None and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                p = json.load(f)
            source = "local disk"
        except Exception as e:
            print("local load error:", e)
    if p:
        try:
            POINTS_CACHE = p.get("points_cache", {})
            HISTORY      = {a: list(s) for a,s in p.get("history", {}).items()}
            PEAK         = p.get("peak", {})
            STREAMER_LOG = p.get("streamer_log", {})
            UPTIME       = p.get("uptime", {})
            CRASH_COUNT  = p.get("crash_count", {})
            SILENCE_LOG  = p.get("silence_log", {})
            NICKNAMES    = p.get("nicknames", {})
            PINNED       = set(p.get("pinned", []))
            EVENT_LOG    = p.get("event_log", [])
            GITHUB_REPOS = {
                acc: {**cfg, "token": decrypt_token(cfg.get("token", ""))}
                for acc, cfg in p.get("github_repos", {}).items()
            }
            print(f"Loaded {len(POINTS_CACHE)} accounts, {len(GITHUB_REPOS)} linked repos from {source}")
        except Exception as e:
            print("load apply error:", e)
    else:
        print("No prior state found — starting fresh")
    if os.path.exists(DAILY_FILE):
        try:
            with open(DAILY_FILE) as f:
                DAILY.update(json.load(f))
        except Exception as e:
            print("daily load error:", e)

def periodic_save():
    while True:
        time.sleep(SAVE_INTERVAL)
        save_data()

def midnight_snapshot():
    while True:
        now = datetime.now(timezone.utc)
        secs = ((24-now.hour-1)*3600 + (60-now.minute-1)*60 + (60-now.second))
        time.sleep(secs+1)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        DAILY[today] = {}
        for acc, info in POINTS_CACHE.items():
            DAILY[today][acc] = dict(info.get("channels", {}))
        save_daily()
        print(f"Daily snapshot: {today}")

# Single load on startup, threads start after
load_data()
# Immediately push whatever we loaded so the backup is always fresh after a redeploy
threading.Thread(target=save_data, daemon=True).start()
threading.Thread(target=periodic_save, daemon=True).start()
threading.Thread(target=midnight_snapshot, daemon=True).start()

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/update", methods=["POST"])
def update():
    data = request.json
    if not data: return jsonify({"error":"no data"}),400
    account         = data.get("account")
    channels        = data.get("channels", {})
    updated         = data.get("updated")
    streamer_status = data.get("streamer_status", {})
    platform        = data.get("platform", "twitch")
    if not account: return jsonify({"error":"missing account"}),400
    now_ts = int(time.time())
    was_silent = account in _silence_start
    if account not in UPTIME:
        UPTIME[account] = now_ts
    if was_silent:
        start = _silence_start.pop(account)
        duration = now_ts - start
        if account not in SILENCE_LOG: SILENCE_LOG[account] = []
        SILENCE_LOG[account].append({"start":start,"end":now_ts,"duration":duration})
        if len(SILENCE_LOG[account]) > 100: SILENCE_LOG[account] = SILENCE_LOG[account][-100:]
        CRASH_COUNT[account] = CRASH_COUNT.get(account, 0) + 1
    prev_channels = POINTS_CACHE.get(account, {}).get("channels", {})
    POINTS_CACHE[account] = {
        "channels": channels, "streamer_status": streamer_status,
        "updated": updated, "first_seen": UPTIME[account],
        "platform": platform
    }
    for ch, pts in channels.items():
        prev_pts = prev_channels.get(ch)
        if prev_pts is None: continue
        delta = pts - prev_pts
        if delta <= 0: continue
        if delta == 10: label = "watched the stream"
        elif delta == 50: label = "claimed the 50 point box"
        elif delta >= 100: label = "claimed a big bonus"
        else: label = "earned points"
        EVENT_LOG.append({"ts": now_ts, "account": account, "streamer": ch, "delta": delta, "total": pts, "label": label})
        if len(EVENT_LOG) > MAX_EVENT_LOG:
            del EVENT_LOG[:len(EVENT_LOG) - MAX_EVENT_LOG]
    if account not in HISTORY: HISTORY[account] = []
    HISTORY[account].append({"ts": int(time.time()*1000), "channels": dict(channels)})
    if len(HISTORY[account]) > MAX_HISTORY:
        HISTORY[account] = HISTORY[account][-MAX_HISTORY:]
    if account not in PEAK: PEAK[account] = {}
    for ch, pts in channels.items():
        if pts > PEAK[account].get(ch, 0):
            PEAK[account][ch] = pts
    prev = _prev_status.get(account, {})
    for streamer, is_online in streamer_status.items():
        was_online = prev.get(streamer)
        if was_online != is_online:
            if streamer not in STREAMER_LOG: STREAMER_LOG[streamer] = []
            STREAMER_LOG[streamer].append({"ts":now_ts,"event":"online" if is_online else "offline"})
            if len(STREAMER_LOG[streamer]) > 200:
                STREAMER_LOG[streamer] = STREAMER_LOG[streamer][-200:]
    _prev_status[account] = dict(streamer_status)
    return jsonify({"ok":True})

def silence_detector():
    while True:
        time.sleep(30)
        now = int(time.time())
        for acc, info in POINTS_CACHE.items():
            age = now - info.get("updated", 0)
            if age > 180 and acc not in _silence_start:
                _silence_start[acc] = info.get("updated", now)

threading.Thread(target=silence_detector, daemon=True).start()

@app.route("/api/points")
def points(): return jsonify(POINTS_CACHE)

@app.route("/api/history/<account>")
def history(account): return jsonify(HISTORY.get(account, []))

@app.route("/api/stats")
def stats():
    now = int(time.time())
    total_points = 0
    silent_accounts = []
    streamer_totals = {}
    for acc, info in POINTS_CACHE.items():
        age = now - info.get("updated", 0)
        if age > 180: silent_accounts.append(acc)
        for ch, pts in info.get("channels", {}).items():
            total_points += pts
            streamer_totals[ch] = streamer_totals.get(ch,0) + pts
    return jsonify({
        "total_accounts": len(POINTS_CACHE),
        "silent_accounts": silent_accounts,
        "silent_count": len(silent_accounts),
        "total_points": total_points,
        "streamer_totals": streamer_totals
    })

@app.route("/api/delta/<account>")
def delta(account):
    snaps = HISTORY.get(account, [])
    if not snaps: return jsonify({"1h":0,"6h":0,"24h":0})
    now_ms = int(time.time()*1000)
    def total_pts(s): return sum(s.get("channels",{}).values())
    def delta_for(mins):
        cutoff = now_ms - mins*60*1000
        old = next((s for s in snaps if s["ts"]>=cutoff), snaps[0])
        return total_pts(snaps[-1]) - total_pts(old)
    return jsonify({"1h":delta_for(60),"6h":delta_for(360),"24h":delta_for(1440)})

@app.route("/api/peak")
def peak(): return jsonify(PEAK)

@app.route("/api/streamer-log")
def streamer_log(): return jsonify(STREAMER_LOG)

@app.route("/api/streamer-log/<streamer>")
def streamer_log_single(streamer): return jsonify(STREAMER_LOG.get(streamer, []))

@app.route("/api/events")
def get_events():
    since = request.args.get("since", type=int)
    account = request.args.get("account")
    limit = request.args.get("limit", type=int) or 200
    events = EVENT_LOG
    if since: events = [e for e in events if e["ts"] > since]
    if account: events = [e for e in events if e["account"] == account]
    events = list(reversed(events))[:limit]
    return jsonify(events)

@app.route("/api/daily")
def daily(): return jsonify(DAILY)

@app.route("/api/uptime")
def uptime():
    now = int(time.time())
    return jsonify({acc:{"first_seen":fs,"uptime_seconds":now-fs} for acc,fs in UPTIME.items()})

@app.route("/api/crash-count")
def crash_count(): return jsonify(CRASH_COUNT)

@app.route("/api/silence-log/<account>")
def silence_log(account): return jsonify(SILENCE_LOG.get(account, []))

@app.route("/api/nicknames", methods=["GET"])
def get_nicknames(): return jsonify(NICKNAMES)

@app.route("/api/nicknames", methods=["POST"])
def set_nickname():
    data = request.json
    account  = data.get("account")
    nickname = data.get("nickname","").strip()
    if not account: return jsonify({"error":"missing account"}),400
    if nickname: NICKNAMES[account] = nickname
    elif account in NICKNAMES: del NICKNAMES[account]
    save_data()
    return jsonify({"ok":True})

@app.route("/api/pinned", methods=["GET"])
def get_pinned(): return jsonify(list(PINNED))

@app.route("/api/pinned", methods=["POST"])
def toggle_pin():
    account = request.json.get("account")
    if not account: return jsonify({"error":"missing account"}),400
    if account in PINNED: PINNED.discard(account)
    else: PINNED.add(account)
    save_data()
    return jsonify({"pinned": account in PINNED})

@app.route("/api/delete/<account>", methods=["DELETE"])
def delete_account(account):
    for store in [POINTS_CACHE, HISTORY, PEAK, UPTIME, CRASH_COUNT, SILENCE_LOG, NICKNAMES]:
        store.pop(account, None)
    PINNED.discard(account)
    _prev_status.pop(account, None)
    _silence_start.pop(account, None)
    for date in DAILY:
        DAILY[date].pop(account, None)
    save_data()
    save_daily()
    return jsonify({"ok":True})

@app.route("/api/activity")
def activity():
    events = []
    for streamer, log in STREAMER_LOG.items():
        for e in log:
            events.append({"streamer":streamer,"ts":e["ts"],"event":e["event"]})
    events.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(events[:50])

@app.route("/health")
def health(): return "OK", 200

# ============================================================
# GITHUB BOT CONTROL
# ============================================================

@app.route("/api/github/repos", methods=["GET"])
def get_github_repos():
    safe = {}
    for acc, cfg in GITHUB_REPOS.items():
        safe[acc] = {"repo": cfg.get("repo",""), "file": cfg.get("file","main.py"), "linked": bool(cfg.get("token"))}
    return jsonify(safe)

@app.route("/api/github/link", methods=["POST"])
def link_github_repo():
    data = request.json
    account = data.get("account")
    repo    = data.get("repo")
    token   = data.get("token")
    file    = data.get("file", "main.py")
    if not all([account, repo, token]):
        return jsonify({"error": "missing fields"}), 400
    repo = normalize_repo(repo)
    if repo.count("/") != 1:
        return jsonify({"error": "repo must be in 'owner/reponame' format"}), 400
    GITHUB_REPOS[account] = {"repo": repo, "token": token, "file": file}
    save_data()
    return jsonify({"ok": True})

@app.route("/api/github/unlink/<account>", methods=["DELETE"])
def unlink_github_repo(account):
    GITHUB_REPOS.pop(account, None)
    save_data()
    return jsonify({"ok": True})

@app.route("/api/github/bulk-link", methods=["POST"])
def bulk_link_github_repos():
    data = request.json
    entries = data.get("entries", [])
    if not entries:
        return jsonify({"error": "no entries"}), 400
    results = {}
    for entry in entries:
        account = entry.get("account")
        repo    = entry.get("repo")
        token   = entry.get("token")
        file    = entry.get("file", "main.py")
        if not all([account, repo, token]):
            results[account or "?"] = "missing fields"
            continue
        repo = normalize_repo(repo)
        if repo.count("/") != 1:
            results[account] = "bad repo format"
            continue
        GITHUB_REPOS[account] = {"repo": repo, "token": token, "file": file}
        results[account] = "ok"
    save_data()
    return jsonify({"ok": True, "results": results})

@app.route("/api/debug/backup-status")
def backup_status():
    return jsonify({
        "configured": bool(STATE_GITHUB_REPO and STATE_GITHUB_TOKEN),
        "repo": STATE_GITHUB_REPO,
        "file": STATE_GITHUB_FILE,
        **STATE_BACKUP_STATUS,
    })

@app.route("/api/github/code/<account>", methods=["GET"])
def get_code(account):
    cfg = GITHUB_REPOS.get(account)
    if not cfg:
        return jsonify({"error": "not linked"}), 404
    try:
        content, sha, err = gh_get_file(cfg)
    except Exception as e:
        return jsonify({"error": f"Unexpected server error: {e}"}), 500
    if err:
        return jsonify({"error": err}), 500
    return jsonify({"content": content, "sha": sha})

@app.route("/api/github/code/<account>", methods=["POST"])
def push_code(account):
    cfg = GITHUB_REPOS.get(account)
    if not cfg:
        return jsonify({"error": "not linked"}), 404
    data = request.json
    new_content = data.get("content")
    message     = data.get("message", "Update bot config from dashboard")
    if not new_content:
        return jsonify({"error": "no content"}), 400
    ok, err = gh_push_file(cfg, new_content, message)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True})

@app.route("/api/github/mass-update", methods=["POST"])
def mass_update():
    from concurrent.futures import ThreadPoolExecutor
    data = request.json
    new_content = data.get("content")
    accounts    = data.get("accounts", [])
    message     = data.get("message", "Mass update from dashboard")
    if not new_content:
        return jsonify({"error": "no content"}), 400
    targets = accounts if accounts else list(GITHUB_REPOS.keys())
    def push_one(acc):
        cfg = GITHUB_REPOS.get(acc)
        if not cfg: return acc, "not linked"
        try:
            ok, err = gh_push_file(cfg, new_content, message)
            return acc, "ok" if ok else (err or "unknown error")
        except Exception as e:
            return acc, "error: " + str(e)
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        for acc, status in executor.map(push_one, targets):
            results[acc] = status
    return jsonify({"results": results})

@app.route("/api/github/patch/<account>", methods=["POST"])
def patch_code(account):
    cfg = GITHUB_REPOS.get(account)
    if not cfg:
        return jsonify({"error": "not linked"}), 404
    data = request.json
    content, sha, err = gh_get_file(cfg)
    if err:
        return jsonify({"error": err}), 500
    original = content
    if "streamers" in data:
        streamers = [s for s in data["streamers"] if s][:3]
        streamer_lines = ",\n        ".join([f'Streamer("{s}")' for s in streamers])
        new_block = f'[\n        {streamer_lines},\n    ]'
        content = re.sub(
            r'twitch_miner\.mine\(\s*\[.*?\]',
            f'twitch_miner.mine({new_block}',
            content, flags=re.DOTALL
        )
    if content == original:
        return jsonify({"ok": True, "note": "no changes detected"})
    ok, err = gh_push_file(cfg, content, data.get("message", "Patch settings from dashboard"))
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
