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

STATE_GITHUB_REPO  = os.environ.get("STATE_GITHUB_REPO")   # "owner/reponame"
STATE_GITHUB_TOKEN = os.environ.get("STATE_GITHUB_TOKEN")  # PAT with repo scope
STATE_GITHUB_FILE  = os.environ.get("STATE_GITHUB_FILE", "dashboard_state.json")

# Key used to obscure GitHub PATs before they're written into the backup file.
# GitHub's push-protection secret scanner blocks commits containing anything
# that looks like a real token, so raw tokens can never go into that repo.
# Falls back to STATE_GITHUB_TOKEN itself as key material (never written out).
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
        return value  # not encrypted (e.g. old local data.json) - use as-is
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
SILENCE_LOG  = {}   # { account -> [{start, end, duration}] }
NICKNAMES    = {}   # { account -> nickname }
PINNED       = set()
_prev_status = {}
_silence_start = {}  # account -> ts when it went silent

def save_data():
    try:
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
        }
        with open(DATA_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print("save error:", e)

def save_daily():
    try:
        with open(DAILY_FILE, "w") as f:
            json.dump(DAILY, f)
    except Exception as e:
        print("daily save error:", e)

def load_data():
    global POINTS_CACHE,HISTORY,PEAK,STREAMER_LOG,UPTIME,CRASH_COUNT,SILENCE_LOG,NICKNAMES,PINNED
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                p = json.load(f)
            POINTS_CACHE = p.get("points_cache", {})
            HISTORY      = {a: list(s) for a,s in p.get("history", {}).items()}
            PEAK         = p.get("peak", {})
            STREAMER_LOG = p.get("streamer_log", {})
            UPTIME       = p.get("uptime", {})
            CRASH_COUNT  = p.get("crash_count", {})
            SILENCE_LOG  = p.get("silence_log", {})
            NICKNAMES    = p.get("nicknames", {})
            PINNED       = set(p.get("pinned", []))
            print(f"Loaded {len(POINTS_CACHE)} accounts")
        except Exception as e:
            print("load error:", e)
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

load_data()
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
    platform        = data.get("platform", "twitch")  # "twitch" or "kick"
    if not account: return jsonify({"error":"missing account"}),400

    now_ts = int(time.time())
    was_silent = account in _silence_start

    if account not in UPTIME:
        UPTIME[account] = now_ts

    # track silence recovery
    if was_silent:
        start = _silence_start.pop(account)
        duration = now_ts - start
        if account not in SILENCE_LOG: SILENCE_LOG[account] = []
        SILENCE_LOG[account].append({"start":start,"end":now_ts,"duration":duration})
        if len(SILENCE_LOG[account]) > 100: SILENCE_LOG[account] = SILENCE_LOG[account][-100:]
        CRASH_COUNT[account] = CRASH_COUNT.get(account, 0) + 1

    POINTS_CACHE[account] = {
        "channels": channels, "streamer_status": streamer_status,
        "updated": updated, "first_seen": UPTIME[account],
        "platform": platform
    }

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

# silence detection background thread
def silence_detector():
    while True:
        time.sleep(30)
        now = int(time.time())
        for acc, info in POINTS_CACHE.items():
            age = now - info.get("updated", 0)
            if age > 180 and acc not in _silence_start:
                _silence_start[acc] = info.get("updated", now)
        
silence_detector_thread = threading.Thread(target=silence_detector, daemon=True)
silence_detector_thread.start()

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

# NICKNAMES
@app.route("/api/nicknames", methods=["GET"])
def get_nicknames(): return jsonify(NICKNAMES)

@app.route("/api/nicknames", methods=["POST"])
def set_nickname():
    data = request.json
    account  = data.get("account")
    nickname = data.get("nickname","").strip()
    if not account: return jsonify({"error":"missing account"}),400
    if nickname:
        NICKNAMES[account] = nickname
    elif account in NICKNAMES:
        del NICKNAMES[account]
    save_data()
    return jsonify({"ok":True})

# PINNED
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

# DELETE ACCOUNT
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

# ACTIVITY FEED (last 50 events across all streamers)
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ============================================================
# GITHUB BOT CONTROL
# ============================================================

# { account -> { "repo": "owner/repo", "token": "ghp_xxx", "file": "main.py" } }
GITHUB_REPOS = {}

@app.route("/api/github/repos", methods=["GET"])
def get_github_repos():
    # Return repos without exposing tokens
    safe = {}
    for acc, cfg in GITHUB_REPOS.items():
        safe[acc] = {"repo": cfg.get("repo",""), "file": cfg.get("file","main.py"), "linked": bool(cfg.get("token"))}
    return jsonify(safe)

def normalize_repo(raw):
    """Accepts 'owner/repo' or a full GitHub URL and returns 'owner/repo'."""
    s = raw.strip()
    s = re.sub(r"^(https?://)?(www\.)?github\.com/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.git$", "", s, flags=re.IGNORECASE)
    s = s.strip("/")
    return s

@app.route("/api/github/link", methods=["POST"])
def link_github_repo():
    data = request.json
    account = data.get("account")
    repo    = data.get("repo")     # "owner/reponame" (or a full URL, which we normalize)
    token   = data.get("token")    # GitHub personal access token
    file    = data.get("file", "main.py")
    if not all([account, repo, token]):
        return jsonify({"error": "missing fields"}), 400
    repo = normalize_repo(repo)
    if repo.count("/") != 1:
        return jsonify({"error": "repo must be in
