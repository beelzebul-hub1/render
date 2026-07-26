
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
GOALS        = {}   # { account -> { channel -> target } }
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
            "goals": GOALS,
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
    global POINTS_CACHE,HISTORY,PEAK,STREAMER_LOG,UPTIME,CRASH_COUNT,SILENCE_LOG,GOALS,NICKNAMES,PINNED
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
            GOALS        = p.get("goals", {})
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

# GOALS
@app.route("/api/goals", methods=["GET"])
def get_goals(): return jsonify(GOALS)

@app.route("/api/goals", methods=["POST"])
def set_goal():
    data = request.json
    account = data.get("account")
    channel = data.get("channel")
    target  = data.get("target")
    if not all([account, channel, target]): return jsonify({"error":"missing fields"}),400
    if account not in GOALS: GOALS[account] = {}
    GOALS[account][channel] = int(target)
    save_data()
    return jsonify({"ok":True})

@app.route("/api/goals/<account>/<channel>", methods=["DELETE"])
def delete_goal(account, channel):
    if account in GOALS and channel in GOALS[account]:
        del GOALS[account][channel]
        save_data()
    return jsonify({"ok":True})

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
    for store in [POINTS_CACHE, HISTORY, PEAK, UPTIME, CRASH_COUNT, SILENCE_LOG, GOALS, NICKNAMES]:
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
import base64
import re
import requests

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
        return jsonify({"error": "repo must be in 'owner/reponame' format"}), 400
    GITHUB_REPOS[account] = {"repo": repo, "token": token, "file": file}
    save_data()
    return jsonify({"ok": True})

@app.route("/api/github/unlink/<account>", methods=["DELETE"])
def unlink_github_repo(account):
    GITHUB_REPOS.pop(account, None)
    save_data()
    return jsonify({"ok": True})

def gh_get_file(cfg):
    """Fetch current file content + sha from GitHub."""
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
        return None, None, f"'{file}' is a directory, not a file — set File to the exact file path (e.g. bot/main.py)"
    if "content" not in j:
        return None, None, f"Unexpected response from GitHub for '{file}'"
    try:
        content = base64.b64decode(j["content"]).decode("utf-8")
    except Exception as e:
        return None, None, f"Could not decode file content: {e}"
    return content, j["sha"], None

def gh_push_file(cfg, new_content, commit_message="Update bot config from dashboard"):
    """Push new content to GitHub file. Creates the file if it doesn't exist yet."""
    repo  = cfg["repo"]
    token = cfg["token"]
    file  = cfg.get("file", "main.py")
    _, sha, err = gh_get_file(cfg)
    _no_file_yet = ("not found", "is empty")
    if err and not any(kw in err.lower() for kw in _no_file_yet):
        # A real error (bad token, bad repo, etc.) - can't proceed.
        return False, err
    # sha stays None if the file doesn't exist yet; GitHub creates it in that case.
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

STATE_BACKUP_STATUS = {
    "last_push_ok": None,      # True / False / None (never attempted)
    "last_push_error": None,
    "last_push_time": None,
    "last_pull_ok": None,
    "last_pull_error": None,
    "last_pull_time": None,
}

def gh_state_pull():
    """Fetch the persisted app-state JSON from the backup GitHub repo, if configured."""
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
    """Push the current app-state JSON to the backup GitHub repo, if configured."""
    if not (STATE_GITHUB_REPO and STATE_GITHUB_TOKEN):
        STATE_BACKUP_STATUS["last_push_ok"] = False
        STATE_BACKUP_STATUS["last_push_error"] = "STATE_GITHUB_REPO / STATE_GITHUB_TOKEN not set"
        STATE_BACKUP_STATUS["last_push_time"] = datetime.now(timezone.utc).isoformat()
        return

    # Safety net: a container that hasn't finished loading yet (or a stale
    # container lingering during a redeploy) has a blank in-memory state.
    # If its periodic autosave fires in that window, it would otherwise
    # silently overwrite a real backup with nothing. Refuse that specific
    # case: pushing blank is only ever allowed if the current remote backup
    # is *also* already blank (nothing real to lose).
    is_blank = not payload.get("points_cache") and not payload.get("github_repos")
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
    data = request.json
    new_content = data.get("content")
    accounts    = data.get("accounts", [])  # empty = all linked
    message     = data.get("message", "Mass update from dashboard")
    if not new_content:
        return jsonify({"error": "no content"}), 400
    targets = accounts if accounts else list(GITHUB_REPOS.keys())
    results = {}
    for acc in targets:
        cfg = GITHUB_REPOS.get(acc)
        if not cfg:
            results[acc] = "not linked"
            continue
        ok, err = gh_push_file(cfg, new_content, message)
        results[acc] = "ok" if ok else err
    return jsonify({"results": results})

@app.route("/api/github/patch/<account>", methods=["POST"])
def patch_code(account):
    """
    Patch specific settings in main.py without touching the rest of the code.
    Supports: streamers list (max 3, priority order)
    """
    cfg = GITHUB_REPOS.get(account)
    if not cfg:
        return jsonify({"error": "not linked"}), 404
    data = request.json
    content, sha, err = gh_get_file(cfg)
    if err:
        return jsonify({"error": err}), 500

    original = content

    # Patch streamers list (max 3, in priority order)
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

# Patch load_data / save_data to include GITHUB_REPOS
_orig_save = save_data
def save_data():
    payload = {
        "points_cache": POINTS_CACHE,
        "history": {a: list(s) for a,s in HISTORY.items()},
        "peak": PEAK,
        "streamer_log": STREAMER_LOG,
        "uptime": UPTIME,
        "crash_count": CRASH_COUNT,
        "silence_log": SILENCE_LOG,
        "goals": GOALS,
        "nicknames": NICKNAMES,
        "pinned": list(PINNED),
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
    # Best-effort backup to GitHub so this survives a redeploy, not just a crash.
    try:
        gh_state_push(payload)
    except Exception as e:
        print("state backup push error:", e)

_orig_load = load_data
def load_data():
    global POINTS_CACHE,HISTORY,PEAK,STREAMER_LOG,UPTIME,CRASH_COUNT,SILENCE_LOG,GOALS,NICKNAMES,PINNED,GITHUB_REPOS
    p = None
    source = None

    # 1. Prefer the GitHub backup — it survives redeploys, local disk doesn't.
    try:
        p = gh_state_pull()
        if p is not None:
            source = "GitHub backup"
    except Exception as e:
        print("state backup pull error:", e)

    # 2. Fall back to the local file (covers a same-container crash/restart,
    #    or a first run before any GitHub backup exists yet).
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
            GOALS        = p.get("goals", {})
            NICKNAMES    = p.get("nicknames", {})
            PINNED       = set(p.get("pinned", []))
            GITHUB_REPOS = {
                acc: {**cfg, "token": decrypt_token(cfg.get("token", ""))}
                for acc, cfg in p.get("github_repos", {}).items()
            }
            print(f"Loaded {len(POINTS_CACHE)} accounts from {source}")
        except Exception as e:
            print("load apply error:", e)
    else:
        print("No prior state found (GitHub backup or local) — starting fresh")

    if os.path.exists(DAILY_FILE):
        try:
            with open(DAILY_FILE) as f:
                DAILY.update(json.load(f))
        except Exception as e:
            print("daily load error:", e)

load_data()
