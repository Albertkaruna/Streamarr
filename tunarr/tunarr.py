#!/usr/bin/env python3
# Tunarr: songs page + worker (systemd user service tunarr.service, http://<pc>:5040).
#   - playlists: YouTube / YouTube Music URLs, read with sockseek (no Soulseek login)
#   - each song is searched in slskd, best FLAC / MP3>=320 queued there (visible in slskd UI),
#     finished files moved from Songs/Manual/... to Songs/<playlist>/
#   - songs Soulseek can't find (3 tries) -> 320/FLAC soundtrack pack via Prowlarr -> qBittorrent
#   - runs 22:00-09:00; "Download now" overrides until the next 09:00 (like qBittorrent's gauge)
import base64, datetime, hashlib, hmac, html, http.cookiejar, json, re, shutil, subprocess, sys, threading, time, uuid
import urllib.parse, urllib.request, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVARR = HERE.parent
STATE_FILE = HERE / "tunarr_state.json"
SONGS = Path("/mnt/data/Media/Songs")
MANUAL = SONGS / "Manual"              # slskd downloads dir (/data/Songs/Manual in the container)
SLSKD = "http://localhost:5030/api/v0"
PORT = 5040
SINGLES = "singles"                    # pseudo-playlist key for individually added songs
WINDOW = (22, 9)                       # full-speed window, same as qBittorrent
CYCLE_MIN = 30                         # while active, search pending songs this often
READ_H = 6                             # re-read playlists this often while active (plus window start / Sync now)
RETRY_H = 24                           # retry "not found" songs after this long
PACK_AFTER = 3                         # soundtrack-pack fallback after this many not-found tries
MAX_PACK, PACK_RETRY_DAYS = 600e6, 7
NOISE = re.compile(r"\b(official|video|lyric(al)?s?|song|full|audio|hd|4k|bass ?boosted|remastered|jukebox|"
                   r"whatsapp status|status|from|movie|film|tamil|songs?|hq|mp3)\b", re.I)
SKIP_WORDS = re.compile(r"remix|karaoke|instrumental|\b8d\b|slowed|reverb|cover|bgm|ringtone", re.I)

lock = threading.Lock()
wake = threading.Event()
sync = threading.Event()               # force a playlist re-read ("Sync now", new playlist)
status = {"msg": "starting", "slskd": "?", "read": "not yet"}


# ---------- state ----------
def load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    st = {"override_until": None, "playlists": {}}
    old = HERE / "playlists.txt"       # one-time migration from the old timer setup
    if old.exists():
        for u in old.read_text().split():
            if u.startswith("http"):
                st["playlists"][u] = {"name": None, "tracks": {}}
    return st


def save(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(STATE_FILE)


def now():
    return datetime.datetime.now()


def next_end(t):
    end = t.replace(hour=WINDOW[1], minute=0, second=0, microsecond=0)
    return end if t < end else end + datetime.timedelta(days=1)


def active(st, t):
    if st["override_until"] and t < datetime.datetime.fromisoformat(st["override_until"]):
        return True
    st["override_until"] = None
    return t.hour >= WINDOW[0] or t.hour < WINDOW[1]


# ---------- helpers ----------
def secret(pattern, path):
    return re.search(pattern, path.read_text(), re.M).group(1)


def http(method, url, body=None, headers=None, timeout=60):
    r = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(r, timeout=timeout) as x:
        t = x.read().decode()
        return json.loads(t) if t.strip() else None


def slskd(method, path, body=None):
    return http(method, SLSKD + path, body, {"X-API-Key": secret(r"^SLSKD_API_KEY=(.+)$", SERVARR / ".env")})


def segments(artist, title, artist_wrong):
    parts = re.split(r"\s*[|]\s*|\s+-\s+|\(|\)|\[|\]", title)
    if not artist_wrong:
        parts = [artist] + parts
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", NOISE.sub(" ", p)).strip(" :,.-")
        if len(p) >= 3 and p.lower() not in (o.lower() for o in out):
            out.append(p)
    return out


def _segs(t):
    return [re.sub(r"[^\w ]", " ", s).strip() for s in segments(t["artist"], t["title"], t.get("artist_wrong"))]


def queries(t):
    segs = _segs(t)
    if not segs:
        return []
    # artist_wrong: segs[0] is the song name (from the title). Otherwise segs[0] is the uploader's
    # "artist" (often the movie) and segs[1] the song - never search the movie name alone.
    song = segs[0] if t.get("artist_wrong") or len(segs) == 1 else segs[1]
    return list(dict.fromkeys(q for q in (" ".join(segs[:2]), song) if q))


def song_names(t):
    # phrases that may be the song title; one must appear in the file NAME (Soulseek matches query
    # words anywhere in the path, e.g. "Oh Baby Girl" hit "OH MY GIRL - SUGAR BABY")
    segs = _segs(t)
    return segs[:1] if t.get("artist_wrong") else segs[:2]


GENERIC = {"music", "songs", "official", "records", "audio", "video", "tamil", "hindi", "telugu", "india",
            "lyrics", "full", "version", "original", "feat", "with"}


def pick(responses, length, query, banned=(), names=(), context=()):
    # FLAC or MP3 >= 320 only; song name in file name; size must fit the song length;
    # prefer FLAC, free slot, short queue, speed
    want = [norm(n) for n in names if norm(n)]
    best, best_key = None, None
    ctx_all = {w for c in context for w in re.findall(r"\w{4,}", c.lower())} - GENERIC
    for r in responses:
        if r["username"] in banned:
            continue
        for f in r["files"]:
            ext = (f.get("extension") or f["filename"].rsplit(".", 1)[-1]).lower()
            if f.get("isLocked") or ext not in ("flac", "mp3"):
                continue
            base = norm(f["filename"].replace("\\", "/").rsplit("/", 1)[-1])
            hit = next((n for n, w in zip(names, want) if w in base), None)
            if want and not hit:
                continue
            # short titles collide ("Oh Baby Girl" by someone else): also need one other clue from the
            # YouTube title (movie, singer, composer) somewhere in the path
            ctx = ctx_all - set(re.findall(r"\w{4,}", (hit or "").lower()))
            if ctx and not any(c in norm(f["filename"]) for c in ctx):
                continue
            if ext == "mp3" and (f.get("bitRate") or 0) < 320:
                continue
            if SKIP_WORDS.search(f["filename"]) and not SKIP_WORDS.search(query):
                continue
            if length:
                lo, hi = (0.5 * 40000, 1.5 * 40000) if ext == "mp3" else (0.5 * 50000, 1.5 * 250000)
                if not lo * length <= f["size"] <= hi * length:
                    continue
            key = (ext == "flac", r.get("hasFreeUploadSlot", False), -(r.get("queueLength") or 0), r.get("uploadSpeed") or 0)
            if best_key is None or key > best_key:
                best, best_key = {"user": r["username"], "file": f["filename"], "size": f["size"]}, key
    return best


def search(text):
    sid = str(uuid.uuid4())
    slskd("POST", "/searches", {"id": sid, "searchText": text, "searchTimeout": 10000})
    for _ in range(40):
        time.sleep(2)
        if slskd("GET", f"/searches/{sid}")["isComplete"]:
            break
    res = slskd("GET", f"/searches/{sid}/responses")
    slskd("DELETE", f"/searches/{sid}")
    return res


def extract(url):
    out = subprocess.run([str(HERE / "bin/sockseek"), "--no-config", url, "--print", "jobs-full"],
                         capture_output=True, text=True, timeout=600).stdout
    name = re.search(r"Job List: running: (.+)", out)
    tracks = {}
    for block in out.split("\n  Song:\n")[1:]:
        g = lambda k: (re.search(rf"^\s+{k}:\s+(.*)$", block, re.M) or [None, ""])[1].strip()
        if g("URL/ID"):
            tracks[g("URL/ID")] = {"artist": g("Artist"), "title": g("Title"),
                                   "length": int(g("Length").rstrip("s") or 0),
                                   "artist_wrong": g("Artist maybe wrong") == "True"}
    return (name.group(1).strip() if name else None), tracks


def safe(name):
    return re.sub(r'[\\/:*?"<>|]', " ", name or "Playlist").strip()


# ---------- soundtrack-pack fallback ----------
def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick_pack(results, seg):
    ok = [r for r in results if r.get("size", 0) <= MAX_PACK and (r.get("seeders") or 0) >= 3
          and re.search(r"320|flac|lossless", r["title"], re.I) and norm(seg) in norm(r["title"])]
    return max(ok, key=lambda r: r.get("seeders") or 0, default=None)


def try_pack(t, playlist):
    key = secret(r"<ApiKey>(\w+)", SERVARR / "config/prowlarr/config.xml")
    for seg in segments(t["artist"], t["title"], t.get("artist_wrong")):
        q = urllib.parse.urlencode({"query": seg, "categories": 3000, "type": "search", "limit": 100})
        best = pick_pack(http("GET", f"http://localhost:9696/api/v1/search?{q}", headers={"X-Api-Key": key}, timeout=300), seg)
        if best:
            jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            post = lambda p, **f: jar.open(urllib.request.Request(f"http://localhost:8080/api/v2/{p}",
                                           urllib.parse.urlencode(f).encode(), headers={"Referer": "http://localhost:8080"}))
            post("auth/login", username="karuna", password=secret(r"password: (\S+)", SERVARR / "credentials.txt"))
            link = best.get("magnetUrl") or best["downloadUrl"].replace("localhost:9696", "prowlarr:9696")
            post("torrents/add", urls=link, category="music", savepath=f"/data/Songs/{safe(playlist)}/_albums", autoTMM="false")
            return True
    return False


# ---------- worker ----------
def collect(st):
    # move finished slskd downloads into their playlist folder; requeue failed ones
    owners = {(t.get("user"), t.get("file")): (pl, t) for pl in st["playlists"].values()
              for t in pl["tracks"].values() if t.get("status") == "queued"}
    for u in slskd("GET", "/transfers/downloads") or []:
        for d in u.get("directories", []):
            for f in d.get("files", []):
                hit = owners.get((u["username"], f["filename"]))
                if not hit or not f["state"].startswith("Completed"):
                    continue
                pl, t = hit
                if f["state"] == "Completed, Succeeded":
                    parts = f["filename"].replace("\\", "/").split("/")
                    src = MANUAL / parts[-2] / parts[-1] if len(parts) > 1 else MANUAL / parts[-1]
                    dst_dir = SONGS / safe(pl["name"])
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    dst, n = dst_dir / parts[-1], 1
                    while dst.exists():
                        dst, n = dst_dir / f"{Path(parts[-1]).stem} ({n}){Path(parts[-1]).suffix}", n + 1
                    if src.exists():
                        shutil.move(str(src), dst)
                        if src.parent != MANUAL and not any(src.parent.iterdir()):
                            src.parent.rmdir()
                        t.update(status="done", path=str(dst.relative_to(SONGS)))
                else:  # errored / rejected / timed out / cancelled -> try another user next time
                    t.setdefault("banned", []).append(u["username"])
                    t.update(status="pending", last=None)


def video_id(url):
    m = re.match(r"https://(?:music\.|www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([\w-]{11})", url) or \
        re.match(r"https://youtu\.be/([\w-]{11})", url)
    return m and m.group(1)


def song_from_oembed(title, author):
    # YouTube Music "Songs" come from "<Artist> - Topic" channels with a clean title; videos usually
    # read "Artist - Song (Official Video)"; otherwise the channel is probably a label (artist_wrong)
    if author.endswith(" - Topic"):
        return {"artist": author[:-8], "title": title, "artist_wrong": False}
    if " - " in title:
        a, s = title.split(" - ", 1)
        return {"artist": a.strip(), "title": s.strip(), "artist_wrong": False}
    return {"artist": author, "title": title, "artist_wrong": True}


def single(url):
    vid = video_id(url)
    q = urllib.parse.urlencode({"format": "json", "url": f"https://www.youtube.com/watch?v={vid}"})
    d = http("GET", f"https://www.youtube.com/oembed?{q}", timeout=30)
    return vid, {**song_from_oembed(d["title"], d["author_name"]), "length": 0}   # oEmbed has no duration


def read_playlists(st):
    # 1-3 min of network/CPU per playlist, so only at window start, every READ_H hours, or on "Sync now"
    for url, pl in list(st["playlists"].items()):
        if url == SINGLES:
            continue
        status["msg"] = f"reading playlist {pl['name'] or url}"
        name, tracks = extract(url)
        with lock:
            if url not in st["playlists"]:
                continue
            pl["name"] = name or pl["name"]
            for vid, t in tracks.items():
                pl["tracks"].setdefault(vid, {**t, "status": "pending", "tries": 0})
            save(st)
    status["read"] = f"{now():%a %H:%M}"


def cycle(st):
    for pl in list(st["playlists"].values()):
        for t in list(pl["tracks"].values()):
            with lock:
                if not active(st, now()):
                    return
            last = t.get("last")
            due = t["status"] == "pending" or (t["status"] == "notfound" and (
                not last or now() - datetime.datetime.fromisoformat(last) > datetime.timedelta(hours=RETRY_H)))
            if not due:
                continue
            status["msg"] = f"searching song {t['tries'] + 1}x in {pl['name']}"
            best = None
            for q in queries(t):
                best = pick(search(q), t["length"], q, t.get("banned", ()), song_names(t), _segs(t))
                if best:
                    break
            with lock:
                t["last"] = now().isoformat(timespec="seconds")
                if best:
                    slskd("POST", f"/transfers/downloads/{urllib.parse.quote(best['user'])}",
                          [{"filename": best["file"], "size": best["size"]}])
                    t.update(status="queued", **best)
                else:
                    t["tries"] += 1
                    t["status"] = "notfound"
                    pack_due = not t.get("pack_tried") or (now() - datetime.datetime.fromisoformat(t["pack_tried"])
                                                           > datetime.timedelta(days=PACK_RETRY_DAYS))
                    if t["tries"] >= PACK_AFTER and pack_due:
                        t["pack_tried"] = t["last"]
                        if try_pack(t, pl["name"]):
                            t["status"] = "torrent"
                save(st)


def worker(st):
    last_cycle = last_read = 0
    was_on = False
    while True:
        try:
            status["slskd"] = slskd("GET", "/server")["state"]
            with lock:
                collect(st)
                save(st)
                on = active(st, now())
            if sync.is_set() or (on and not was_on) or (on and time.time() - last_read > READ_H * 3600):
                sync.clear()
                read_playlists(st)
                last_read = time.time()
            was_on = on
            if on and (wake.is_set() or time.time() - last_cycle > CYCLE_MIN * 60):
                wake.clear()
                cycle(st)
                last_cycle = time.time()
            status["msg"] = "active, waiting for next search round" if on else "idle until 22:00"
        except Exception as e:
            status["msg"] = f"error: {e}"[:200]
        wake.wait(60)


# ---------- sign-in page (replaces the browser's basic-auth popup) ----------
LOGIN = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Sign in · Tunarr</title>
<meta name="theme-color" content="#0b1020"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg">
<script>addEventListener("DOMContentLoaded",()=>{document.querySelector("form").action="/login"+location.search})</script>
<style>
:root{--bg:#0b1020;--line:#283056;--fg:#e8eaf6;--mut:#9aa3c0}
*{box-sizing:border-box}html,body{margin:0;min-height:100vh;color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background:radial-gradient(900px 600px at 10% -10%,rgba(99,102,241,.3),transparent 60%),radial-gradient(900px 700px at 100% 110%,rgba(34,211,238,.18),transparent 60%),var(--bg) fixed}
main{min-height:100vh;display:grid;place-items:center;padding:24px 16px}
form{width:100%;max-width:360px;background:rgba(15,18,42,.82);border:1px solid rgba(129,140,248,.18);border-radius:16px;padding:32px 26px;box-shadow:0 20px 60px rgba(0,0,0,.4);backdrop-filter:blur(12px)}
.logo{display:block;width:72px;height:72px;margin:0 auto 14px;border-radius:20px;filter:drop-shadow(0 10px 28px rgba(99,102,241,.5))}
h1{margin:0;text-align:center;font-size:28px;font-weight:800;letter-spacing:-.02em;background:linear-gradient(90deg,#c7d2fe,#67e8f9);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{text-align:center;color:var(--mut);font-size:12px;letter-spacing:.1em;text-transform:uppercase;margin:2px 0 24px}
label{display:block;font-size:12px;font-weight:600;color:var(--mut);margin:14px 0 6px}
input{width:100%;font:inherit;color:var(--fg);background:rgba(11,16,32,.9);border:1px solid var(--line);border-radius:10px;padding:11px 13px;outline:none}
input:focus{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.25)}
button{width:100%;margin-top:22px;font:inherit;font-weight:700;color:#fff;border:0;border-radius:10px;padding:12px;cursor:pointer;background:linear-gradient(90deg,#6366f1,#06b6d4)}
button:hover{filter:brightness(1.1)}
.err{margin:16px 0 0;padding:9px 12px;border-radius:8px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);color:#fca5a5;font-size:13px;text-align:center}
.foot{text-align:center;color:var(--mut);font-size:12px;margin-top:18px;opacity:.7}
</style></head><body><main><form method="post" action="/login">
<img class="logo" src="/icon.svg" alt=""><h1>Tunarr</h1><div class="sub">by Streamarr</div>
@@err@@
<label for="u">Username</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required autofocus>
<label for="p">Password</label><input id="p" name="pw" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button><div class="foot">Livin' the Stream Life</div>
</form></main></body></html>"""


# ---------- web page (dark, slskd / Semantic-UI-like, tabs) ----------
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Tunarr</title>
<meta name="theme-color" content="#0b1020"><link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg">
<style>
:root{--bg:#0b1020;--seg:rgba(20,26,48,.82);--seg2:#1c2340;--line:#283056;--fg:#e8eaf6;--mut:#9aa3c0;
--blue:#6366f1;--green:#22c55e;--red:#ef4444;--orange:#f97316;--teal:#22d3ee;--grey:#5b6385;--purple:#a855f7}
*{box-sizing:border-box}html,body{margin:0;background:radial-gradient(900px 600px at 10% -10%,rgba(99,102,241,.25),transparent 60%),radial-gradient(900px 700px at 100% 110%,rgba(34,211,238,.16),transparent 60%),var(--bg) fixed;color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
html{background:#0b1020}body{min-height:100vh;background-attachment:fixed}
a{color:#67e8f9;text-decoration:none}a:hover{text-decoration:underline}
.bar{position:sticky;top:0;z-index:6;display:flex;align-items:center;gap:4px;background:rgba(8,11,24,.82);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:0 16px;height:52px}
.brand{font-weight:700;font-size:18px;margin-right:auto;display:flex;align-items:center;gap:10px;color:var(--fg)}.brand:hover{text-decoration:none;opacity:.85}
.brand .logo{width:30px;height:30px;border-radius:8px;filter:drop-shadow(0 4px 12px rgba(99,102,241,.5))}.brand b{background:linear-gradient(90deg,#c7d2fe,#67e8f9);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800;letter-spacing:-.01em}.brand small{display:block;font-size:10px;font-weight:600;letter-spacing:.08em;color:var(--mut);text-transform:uppercase;margin-top:-3px}
.bar a.item{color:var(--fg);padding:8px 12px;border-radius:6px;font-size:14px}.bar a.item:hover{background:var(--seg2);text-decoration:none}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
.tabs{position:sticky;top:52px;z-index:5;display:flex;justify-content:center;gap:4px;background:rgba(10,14,30,.85);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:6px 10px}
.tabs a{display:flex;align-items:center;gap:7px;color:var(--mut);padding:8px 16px;border-radius:6px;font-weight:700;font-size:14px;position:relative}
.tabs a:hover{text-decoration:none;background:var(--seg2)}.tabs a.on{color:var(--fg);background:var(--seg2);box-shadow:inset 0 -2px 0 var(--teal)}
.badge{min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--blue);color:#fff;font-size:11px;display:inline-grid;place-items:center}
.badge:empty{display:none}.pulse{animation:pu .9s ease 2}@keyframes pu{50%{transform:scale(1.45);background:var(--green)}}
main{max-width:980px;margin:0 auto;padding:18px 16px 40px}.pane{display:none}.pane.on{display:block}
.seg{background:var(--seg);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:0 0 16px}
.seg h3{margin:0 0 10px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);display:flex;align-items:center;gap:8px}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.btn{font:inherit;font-weight:700;font-size:14px;padding:9px 16px;border-radius:6px;border:0;cursor:pointer;color:#fff;background:var(--grey)}
.btn:hover{filter:brightness(1.12)}.btn:disabled{opacity:.55;cursor:default;filter:none}
.btn.blue{background:linear-gradient(135deg,var(--blue),#4f8df5)}.btn.orange{background:var(--orange)}.btn.green{background:var(--green)}
.btn.basic{background:transparent;color:var(--fg);box-shadow:inset 0 0 0 1px var(--line)}.btn.basic:hover{background:var(--seg2)}
.btn.red.basic{color:#ff7b7b;box-shadow:inset 0 0 0 1px #6b2a2a}.btn.sm{padding:5px 10px;font-size:12px}
.lbl{display:inline-block;font-size:12px;font-weight:700;line-height:1;padding:5px 9px;border-radius:4px;color:#fff;background:var(--grey);white-space:nowrap}
.lbl.done,.lbl.ok{background:var(--green)}.lbl.queued,.lbl.active{background:var(--blue)}.lbl.pending,.lbl.waiting{background:var(--grey)}
.lbl.notfound,.lbl.failed{background:var(--red)}.lbl.torrent{background:var(--orange)}.lbl.mode{background:var(--teal)}.lbl.now{background:var(--orange)}
.lbl.flac{background:var(--purple)}.lbl.mp3{background:var(--blue)}
.chip{font:inherit;font-size:13px;font-weight:700;padding:6px 12px;border-radius:16px;border:1px solid var(--line);background:transparent;color:var(--mut);cursor:pointer}
.chip.on{background:var(--blue);border-color:var(--blue);color:#fff}
select{font:inherit;font-size:13px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:0 0 16px}
.stat{background:var(--seg);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.stat b{display:block;font-size:26px;line-height:1.1}.stat span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.stat.done b{color:var(--green)}.stat.queued b{color:#4ea8ff}.stat.notfound b{color:#ff7b7b}.stat.torrent b{color:var(--orange)}
input[type=url],input[type=search]{flex:1;min-width:200px;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:9px 12px}
input[type=url]:focus,input[type=search]:focus{outline:none;border-color:var(--blue)}
.mut{color:var(--mut)}.small{font-size:13px}
.pl-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.pl-head .name{font-size:17px;font-weight:700;margin-right:auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px}
details{margin-top:10px}summary{cursor:pointer;color:var(--mut);font-size:13px;list-style:none}summary::before{content:"▸ ";}
details[open] summary::before{content:"▾ "}
table{width:100%;border-collapse:collapse;margin-top:8px}
td{padding:8px 6px;border-top:1px solid var(--line);vertical-align:middle;word-break:break-word}
td.st{width:1%;text-align:right;white-space:nowrap}td .artist{color:var(--mut);font-size:13px}
form{display:inline;margin:0}
.grp{border-top:1px solid var(--line);padding:12px 0}.grp:first-child{border-top:0;padding-top:4px}
.grp-h{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.grp-h .dir{font-weight:700;margin-right:auto;word-break:break-word}
.file{display:flex;align-items:center;gap:8px;padding:6px 0 6px 10px;border-left:2px solid var(--line);margin-top:6px}
.file .fn{flex:1;min-width:0;word-break:break-word;font-size:14px}
.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--blue);border-radius:50%;display:inline-block;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.dl{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}.dl:first-child{border-top:0}
.dl .info{flex:1;min-width:0}.dl .fn{word-break:break-word;font-size:14px}
.prog{height:5px;background:var(--seg2);border-radius:3px;margin-top:6px;overflow:hidden}.prog i{display:block;height:100%;background:var(--blue)}
.dl.ok .prog i{background:var(--green)}.dl.failed .prog i{background:var(--red)}
.hide{display:none!important}
@media (max-width:640px){
 .bar{padding:0 10px}.bar a.item{padding:8px;font-size:13px}.brand{font-size:16px}
 .tabs{position:fixed;top:auto;bottom:0;left:0;right:0;justify-content:space-around;border-top:1px solid var(--line);border-bottom:0;padding:4px 4px calc(4px + env(safe-area-inset-bottom))}
 .tabs a{flex-direction:column;gap:1px;font-size:11px;padding:6px 10px;flex:1}.tabs a .ic{font-size:19px}
 .tabs a .badge{position:absolute;top:2px;right:calc(50% - 26px)}.tabs a.on{box-shadow:none;color:#4ea8ff}
 main{padding-bottom:90px}.stats{gap:6px}.stat{padding:8px 6px;text-align:center}.stat b{font-size:20px}.stat span{font-size:9px;letter-spacing:0}
 .dl{flex-wrap:wrap}.dl .acts{width:100%;text-align:right}}
</style></head><body>
<div class="bar"><a class="brand" href="/#search" title="Home"><img class="logo" src="/icon.svg" alt=""><span><b>Tunarr</b><small>by Streamarr</small></span></a>
<a class="item" href="@@slskd_url@@" title="slskd: browse users, all transfers"><span class="dot" style="background:@@dot@@"></span>slskd</a>
<a class="item" href="@@qbt_url@@">qBittorrent</a>
<a class="item" href="/logout" title="Sign out">Sign out</a></div>
<nav class="tabs"><a href="#search" data-tab="search"><span class="ic">🔍</span>Search</a>
<a href="#playlists" data-tab="playlists"><span class="ic">📋</span>Playlists</a>
<a href="#downloads" data-tab="downloads"><span class="ic">⬇</span>Downloads<span class="badge" id="badge"></span></a></nav>
<main>
@@main@@
</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const mb=b=>(b/1e6).toFixed(1)+" MB", api=u=>fetch(location.origin+u).then(r=>r.json()),
 post=(u,d)=>fetch(location.origin+u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)}).then(r=>r.json());

// ---- tabs (remembered; #hash links work) ----
function show(t){if(!["search","playlists","downloads"].includes(t))t="search";
 $$(".pane").forEach(p=>p.classList.toggle("on",p.id==="p-"+t)); $$(".tabs a").forEach(a=>a.classList.toggle("on",a.dataset.tab===t));
 try{localStorage.setItem("tab",t)}catch(e){} if(t==="downloads")loadDl()}
addEventListener("hashchange",()=>show(location.hash.slice(1)));
let saved="search";try{saved=localStorage.getItem("tab")||"search"}catch(e){}
show(location.hash.slice(1)||saved);

// ---- live HD search with filters ----
let raw=[], groups=[], last={complete:true,responses:0}, poll=null;
const F={flac:false,mp3:false,slot:false,sort:"best"};
function applyFilters(){groups=raw.map(g=>({...g,files:g.files.filter(f=>(!F.flac||f.ext=="flac")&&(!F.mp3||f.ext=="mp3"))}))
 .filter(g=>g.files.length&&(!F.slot||g.slot));
 const size=g=>g.files.reduce((a,f)=>a+f.size,0);
 if(F.sort=="speed")groups.sort((a,b)=>b.speed-a.speed); else if(F.sort=="size")groups.sort((a,b)=>size(b)-size(a));}
function renderSearch(){const r=last; $("#results").classList.remove("hide"); applyFilters();
 const n=groups.reduce((a,g)=>a+g.files.length,0);
 $("#rhead").innerHTML=(r.complete?"":"<span class='spin'></span>")+`${n} HD files in ${groups.length} folders`+
  (r.complete?(raw.length&&groups.length<raw.length?` <span class="mut">(filtered from ${raw.length})</span>`:""):` · searching… ${r.responses} users answered`);
 $("#rlist").innerHTML=groups.length?groups.map((g,gi)=>`<div class="grp"><div class="grp-h"><span class="dir">${esc(g.dir.split("/").pop()||g.dir)}</span>
  ${g.files.length>1?`<button class="btn basic sm" data-g="${gi}">Download all ${g.files.length} (${(g.files.reduce((a,f)=>a+f.size,0)/1e6).toFixed(0)} MB)</button>`:""}</div>
  <div class="mut small">${esc(g.user)} · ${(g.speed/1e6).toFixed(1)} MB/s · ${g.slot?"<span class='lbl ok'>free slot</span>":`<span class='lbl waiting'>queue ${g.queue}</span>`}</div>
  ${g.files.map((f,fi)=>`<div class="file"><span class="lbl ${f.ext}">${f.ext=="flac"?"FLAC":f.br+" kbps"}</span><span class="fn">${esc(f.name)}</span>
  <span class="mut small">${mb(f.size)}</span><button class="btn blue sm" data-g="${gi}" data-f="${fi}">Download</button></div>`).join("")}</div>`).join("")
  :(r.complete?"<div class='mut'>No matching FLAC / 320 kbps results. Try the movie or composer name, another spelling, or fewer filters.</div>":"");
 markQueued()}
const queued=new Set();
function markQueued(){$$("#rlist button[data-f]").forEach(b=>{const f=groups[b.dataset.g].files[b.dataset.f];
 if(queued.has(groups[b.dataset.g].user+"|"+f.file)){b.textContent="Queued ✓";b.disabled=true;b.className="btn green sm"}})}
async function runSearch(q){q=q.trim(); if(!q)return; clearInterval(poll); busy=true; $("#q").value=q; show("search");
 raw=[]; last={complete:false,responses:0}; renderSearch();
 const sid=(await post("/api/search",{q})).sid;
 const tick=async()=>{if(document.hidden)return; const r=await api("/api/search?sid="+sid); raw=r.groups; last=r; renderSearch(); if(r.complete)clearInterval(poll)};
 poll=setInterval(tick,1500); tick();
 try{const h=JSON.parse(localStorage.getItem("recent")||"[]").filter(x=>x.toLowerCase()!==q.toLowerCase()); h.unshift(q); localStorage.setItem("recent",JSON.stringify(h.slice(0,8)))}catch(e){}
 recent()}
function recent(){let h=[];try{h=JSON.parse(localStorage.getItem("recent")||"[]")}catch(e){}
 $("#recent").innerHTML=h.length?"<span class='mut small'>Recent:</span> "+h.map(x=>`<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join(" "):""}
$("#sform").addEventListener("submit",ev=>{ev.preventDefault(); runSearch($("#q").value)});
$("#recent").addEventListener("click",ev=>{const b=ev.target.closest("[data-q]"); if(b)runSearch(b.dataset.q)});
$("#filters").addEventListener("click",ev=>{const b=ev.target.closest("[data-f]"); if(!b)return; F[b.dataset.f]=!F[b.dataset.f];
 if(b.dataset.f=="flac"&&F.flac)F.mp3=false; if(b.dataset.f=="mp3"&&F.mp3)F.flac=false;
 $$("#filters [data-f]").forEach(x=>x.classList.toggle("on",F[x.dataset.f])); renderSearch()});
$("#sort").addEventListener("change",ev=>{F.sort=ev.target.value; renderSearch()});
$("#rlist").addEventListener("click",async ev=>{const b=ev.target.closest("button[data-g]"); if(!b)return;
 const g=groups[b.dataset.g], files=b.dataset.f!==undefined?[g.files[b.dataset.f]]:g.files;
 b.disabled=true; const r=await post("/api/dl",{user:g.user,files:files.map(f=>({filename:f.file,size:f.size}))});
 if(r.ok){files.forEach(f=>queued.add(g.user+"|"+f.file)); if(b.dataset.f===undefined){b.textContent="Queued ✓";b.className="btn green sm"}
  markQueued(); const bd=$("#badge"); bd.classList.remove("pulse"); void bd.offsetWidth; bd.classList.add("pulse"); loadDl()}
 else{b.disabled=false; b.textContent="Failed – retry"}});
// "Search manually" on not-found playlist songs
document.addEventListener("click",ev=>{const b=ev.target.closest("[data-search]"); if(b){ev.preventDefault(); runSearch(b.dataset.search)}});
recent();
// ?q=… (from Discover's "+ Tunarr") → run that search right away
{const q0=new URLSearchParams(location.search).get("q");
 if(q0){runSearch(q0); history.replaceState(null,"",location.pathname+"#search")}}

// ---- live downloads (paused while the tab/screen is hidden) ----
async function loadDl(){if(document.hidden)return; let r; try{r=await api("/api/downloads")}catch(e){return}
 const act=r.items.filter(x=>x.kind=="active"||x.kind=="waiting").length;
 $("#badge").textContent=act||""; $("#dlhead").innerHTML=(act?"<span class='spin'></span>":"")+`Downloads · ${act} active`;
 $("#clearall").classList.toggle("hide",!r.items.some(x=>x.kind=="ok"));
 $("#dl").innerHTML=r.items.length?r.items.map(x=>`<div class="dl ${x.kind}"><div class="info"><div class="fn">${esc(x.name)}</div>
  <div class="mut small">${esc(x.src)} · ${esc(x.user)} · ${mb(x.size)}${x.kind=="active"&&x.speed?` · ${(x.speed/1e6).toFixed(2)} MB/s`:""}${x.queue?` · queue position ${x.queue}`:""}</div>
  <div class="prog"><i style="width:${x.pct}%"></i></div></div><span class="lbl ${x.kind}">${esc(x.label)}</span><span class="acts">
  ${x.kind=="ok"||x.kind=="failed"?"":`<button class="btn red basic sm" data-a="cancel" data-u="${esc(x.user)}" data-id="${x.id}">Cancel</button>`}
  ${x.kind=="failed"?`<button class="btn blue sm" data-a="retry" data-u="${esc(x.user)}" data-id="${x.id}">Retry</button>`:""}
  ${x.kind=="ok"||x.kind=="failed"?`<button class="btn basic sm" data-a="clear" data-u="${esc(x.user)}" data-id="${x.id}" title="Remove from list (file stays)">✕</button>`:""}
  </span></div>`).join(""):"<div class='mut'>Nothing downloading.</div>"}
$("#dl").addEventListener("click",async ev=>{const b=ev.target.closest("button[data-a]"); if(!b)return; b.disabled=true;
 await post("/api/transfer",{action:b.dataset.a,user:b.dataset.u,id:b.dataset.id}); loadDl()});
$("#clearall").addEventListener("click",async ev=>{ev.target.disabled=true; await post("/api/transfer",{action:"clearfinished"}); ev.target.disabled=false; loadDl()});
loadDl(); setInterval(loadDl,3000); document.addEventListener("visibilitychange",()=>{if(!document.hidden)loadDl()});

// ---- Playlists tab: full refresh every 30 s while it's open and idle; keeps open song lists ----
const K="songs-open";try{JSON.parse(sessionStorage.getItem(K)||"[]").forEach(i=>{const d=document.getElementById(i);if(d)d.open=true})}catch(e){}
var busy=false;
setInterval(()=>{if(document.hidden||busy||!$("#p-playlists").classList.contains("on")||$$("input").some(i=>i.value||document.activeElement===i))return;
 try{sessionStorage.setItem(K,JSON.stringify($$("details[open]").map(d=>d.id)))}catch(e){} location.reload()},30000);
</script></body></html>"""

MAIN = """<section class="pane" id="p-search">
<div class="seg"><h3>HD search · FLAC &amp; 320 kbps only</h3><form id="sform" class="row" style="display:flex">
<input type="search" id="q" required placeholder="Song, movie or composer" autocomplete="off"><button class="btn blue">Search</button></form>
<div id="recent" class="row" style="margin-top:10px;gap:6px"></div></div>
<div id="results" class="seg hide"><div class="row" id="filters" style="gap:6px;margin-bottom:10px">
<button class="chip" data-f="flac">FLAC only</button><button class="chip" data-f="mp3">320 kbps only</button>
<button class="chip" data-f="slot">Free slot</button><span style="margin-left:auto" class="mut small">Sort
<select id="sort"><option value="best">Best</option><option value="speed">Fastest</option><option value="size">Largest</option></select></span></div>
<div id="rhead" class="mut small" style="display:flex;align-items:center;gap:8px"></div>
<div id="rlist" style="margin-top:6px"></div>
<div class="mut small" style="margin-top:8px">Saved to Media/Songs/Manual/ · starts right away (not limited to 22:00–09:00).</div></div>
</section>
<section class="pane" id="p-playlists">
<div class="seg"><div class="row">@@mode@@<span class="mut small" style="margin-right:auto">@@mode_note@@</span>
<form method="post"><input type="hidden" name="a" value="@@tog@@"><button class="btn @@tcls@@">@@tlabel@@</button></form>
<form method="post"><input type="hidden" name="a" value="sync"><button class="btn basic">Sync now</button></form>
<form method="post"><input type="hidden" name="a" value="retry"><button class="btn basic">Retry not-found</button></form></div>
<div class="mut small" style="margin-top:10px">Worker: @@msg@@ · playlists last read: @@read@@ · Soulseek: @@slskd@@</div></div>
<div class="stats">@@stats@@</div>
<div class="seg"><h3>Add</h3><form method="post" class="row" style="display:flex"><input type="hidden" name="a" value="add">
<input type="url" name="url" required placeholder="YouTube Music song or playlist link"><button class="btn blue">Add</button></form>
<div class="mut small" style="margin-top:8px">Playlists are re-read when the night window starts and every 6 h (or tap <b>Sync now</b>) — just add songs
in YouTube Music. Single song links go to <b>Singles</b>. Songs from the YouTube Music <b>Songs</b> tab match best.</div></div>
@@playlists@@
<div class="mut small">Files: Media/Songs/&lt;playlist&gt;/</div>
</section>
<section class="pane" id="p-downloads">
<div class="seg"><div class="row" style="margin-bottom:6px"><h3 id="dlhead" style="margin:0 auto 0 0">Downloads</h3>
<button class="btn basic sm hide" id="clearall">Clear finished</button></div><div id="dl"><span class="mut">Loading…</span></div></div>
<div class="mut small">Search downloads: Media/Songs/Manual/ · playlist songs: Media/Songs/&lt;playlist&gt;/</div>
</section>
"""

LABELS = {"done": "Done", "queued": "Downloading", "pending": "Pending", "notfound": "Not found", "torrent": "Torrent"}

MANIFEST = {"name": "Tunarr", "short_name": "Tunarr", "start_url": "/", "display": "standalone",
            "background_color": "#0b1020", "theme_color": "#0b1020",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>'
        '<rect width="96" height="96" rx="26" fill="url(#g)"/>'
        '<path d="M58 22v28a8 8 0 1 1-5-7.4V31l-14 3v18a8 8 0 1 1-5-7.4V26z" fill="#fff"/>'
        '<path d="M22 70c8-6 16-6 24 0s16 6 24 0M22 80c8-6 16-6 24 0s16 6 24 0" fill="none" stroke="#fff" stroke-width="5"'
        ' stroke-linecap="round" opacity=".85"/></svg>')


def render(st, host):
    t = now()
    e = html.escape
    with lock:
        on = active(st, t)
        if st["override_until"]:
            mode = "<span class='lbl now'>DOWNLOAD NOW</span>"
            note = f"until {datetime.datetime.fromisoformat(st['override_until']):%a %H:%M}, then back to schedule"
        else:
            mode = f"<span class='lbl mode'>{'DOWNLOADING' if on else 'SCHEDULED'}</span>"
            note = f"runs {WINDOW[0]}:00–{WINDOW[1]:02d}:00" + ("" if on else " · waiting")
        total = {s: 0 for s in LABELS}
        blocks = []
        items = sorted(st["playlists"].items(), key=lambda kv: kv[0] != SINGLES)
        for url, pl in items:
            tr = pl["tracks"]
            c = {s: sum(1 for x in tr.values() if x["status"] == s) for s in LABELS}
            for s in c:
                total[s] += c[s]
            rows = []
            for vid, x in tr.items():
                qs = queries(x)
                manual = (f" <button class='btn basic sm' data-search='{e(qs[-1])}' title='Search this song manually'>🔍</button>"
                          if x["status"] == "notfound" and qs else "")
                rm = (f" <form method='post'><input type='hidden' name='a' value='rmsong'><input type='hidden' name='vid' value='{e(vid)}'>"
                      f"<button class='btn red basic sm' title='Remove'>✕</button></form>" if url == SINGLES else "")
                rows.append(f"<tr><td>{e(x['title'])}<br><span class='artist'>{e(x['artist'])}</span></td>"
                            f"<td class='st'><span class='lbl {x['status']}'>{LABELS.get(x['status'], x['status'])}"
                            f"{' ×' + str(x['tries']) if x['status'] == 'notfound' else ''}</span>{manual}{rm}</td></tr>")
            chips = "".join(f"<span class='lbl {s}'>{n} {LABELS[s]}</span>" for s, n in c.items() if n)
            src = ("<span class='mut small'>songs added one by one</span>" if url == SINGLES
                   else f"<a class='small' href='{e(url)}'>open playlist ↗</a>")
            rm = ("" if url == SINGLES else
                  f"<form method='post'><input type='hidden' name='a' value='remove'><input type='hidden' name='url' value='{e(url)}'>"
                  f"<button class='btn red basic sm'>Remove</button></form>")
            blocks.append(
                f"<div class='seg'><div class='pl-head'><span class='name'>{e(pl['name'] or 'Loading…')}</span>"
                f"<span class='mut small'>{len(tr)} songs</span>{rm}</div><div class='chips'>{chips}</div>{src}"
                f"<details id='d{zlib.crc32(url.encode())}'><summary>Show songs</summary><table>{''.join(rows)}</table></details></div>")
    h = e(host.split(":")[0])
    stats = "".join(f"<div class='stat {s}'><b>{n}</b><span>{LABELS[s]}</span></div>" for s, n in total.items())
    connected = "LoggedIn" in status["slskd"]
    vals = {"slskd_url": f"http://{h}:5030", "qbt_url": f"http://{h}:8080", "dot": "var(--green)" if connected else "var(--red)",
            "mode": mode, "mode_note": e(note), "msg": e(status["msg"]), "slskd": e(status["slskd"]), "read": e(status["read"]),
            "tog": "off" if st["override_until"] else "on", "tcls": "orange" if st["override_until"] else "blue",
            "tlabel": "Stop (back to schedule)" if st["override_until"] else "Download now",
            "stats": stats,
            "playlists": "".join(blocks) or "<div class='seg mut'>Nothing added yet — paste a YouTube Music link above.</div>"}
    vals["main"] = fill(MAIN, vals)
    return fill(PAGE, vals)


def fill(tpl, vals):
    return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], tpl)


# ---------- HD search (live) ----------
SEARCHES = []                          # recent slskd search ids, oldest first; older ones are deleted in slskd
SEARCH_CACHE = {}                      # lower(query) -> (sid, started); same query within 10 min reuses the results
CACHE_S = 600


def hd_groups(responses):
    # FLAC or MP3 >= 320 only, grouped by (user, folder); best groups first
    groups = []
    for r in responses:
        by_dir = {}
        for f in r["files"]:
            ext = (f.get("extension") or f["filename"].rsplit(".", 1)[-1]).lower()
            if f.get("isLocked") or ext not in ("flac", "mp3") or (ext == "mp3" and (f.get("bitRate") or 0) < 320):
                continue
            d, _, name = f["filename"].replace("\\", "/").rpartition("/")
            by_dir.setdefault(d, []).append({"file": f["filename"], "name": name, "size": f["size"], "ext": ext,
                                             "br": f.get("bitRate")})
        for d, files in by_dir.items():
            files.sort(key=lambda x: x["name"].lower())
            groups.append({"user": r["username"], "dir": d, "files": files,
                           "slot": r.get("hasFreeUploadSlot", False), "queue": r.get("queueLength") or 0,
                           "speed": r.get("uploadSpeed") or 0})
    groups.sort(key=lambda g: (any(x["ext"] == "flac" for x in g["files"]), g["slot"], -g["queue"], g["speed"]), reverse=True)
    return groups[:60]


def start_search(q):
    key = q.lower()
    hit = SEARCH_CACHE.get(key)
    if hit and time.time() - hit[1] < CACHE_S and hit[0] in SEARCHES:
        return hit[0]
    sid = str(uuid.uuid4())
    slskd("POST", "/searches", {"id": sid, "searchText": q[:120], "searchTimeout": 10000})
    SEARCHES.append(sid)
    SEARCH_CACHE[key] = (sid, time.time())
    while len(SEARCHES) > 10:
        old = SEARCHES.pop(0)
        for k in [k for k, v in SEARCH_CACHE.items() if v[0] == old]:
            SEARCH_CACHE.pop(k)
        try:
            slskd("DELETE", f"/searches/{old}")
        except Exception:
            pass
    return sid


def poll_search(sid):
    s = slskd("GET", f"/searches/{sid}")
    return {"complete": s["isComplete"], "responses": s.get("responseCount", 0),
            "groups": hd_groups(slskd("GET", f"/searches/{sid}/responses")) if s["isComplete"] else []}


# ---------- live downloads ----------
def dl_kind(state):
    if state == "Completed, Succeeded":
        return "ok", "Done"
    if state.startswith("Completed"):
        return "failed", state.split(", ", 1)[-1]          # Errored / Rejected / TimedOut / Cancelled
    if state.startswith("InProgress") or state.startswith("Initializing"):
        return "active", "Downloading"
    return "waiting", "Queued" if "Queued" in state else state.split(", ")[0]


def downloads_view(st):
    with lock:
        owners = {(t.get("user"), t.get("file")): pl["name"] or "Playlist" for pl in st["playlists"].values()
                  for t in pl["tracks"].values() if t.get("user")}
    items = []
    for u in slskd("GET", "/transfers/downloads") or []:
        for d in u.get("directories", []):
            for f in d.get("files", []):
                kind, label = dl_kind(f["state"])
                items.append({"id": f["id"], "user": u["username"], "name": f["filename"].replace("\\", "/").rsplit("/", 1)[-1],
                              "size": f.get("size", 0), "pct": round(f.get("percentComplete") or 0, 1),
                              "speed": f.get("averageSpeed") or 0, "queue": f.get("placeInQueue"), "kind": kind, "label": label,
                              "src": owners.get((u["username"], f["filename"]), "Search"),
                              "t": f.get("endedAt") or f.get("startedAt") or f.get("requestedAt") or ""})
    order = {"active": 0, "waiting": 1, "failed": 2, "ok": 3}
    items.sort(key=lambda x: x["t"], reverse=True)
    items.sort(key=lambda x: order[x["kind"]])
    return {"items": items[:60]}


def transfer_action(st, action, user=None, tid=None):
    if action == "clearfinished":
        for u in slskd("GET", "/transfers/downloads") or []:
            for d in u["directories"]:
                for f in d["files"]:
                    if f["state"] == "Completed, Succeeded":
                        slskd("DELETE", f"/transfers/downloads/{urllib.parse.quote(u['username'])}/{f['id']}?remove=true")
        return
    path = f"/transfers/downloads/{urllib.parse.quote(user)}/{urllib.parse.quote(tid)}"
    if action == "cancel":
        slskd("DELETE", path)
    elif action == "clear":
        slskd("DELETE", path + "?remove=true")
    elif action == "retry":
        f = next(f for u in slskd("GET", "/transfers/downloads") if u["username"] == user
                 for d in u["directories"] for f in d["files"] if f["id"] == tid)
        slskd("DELETE", path + "?remove=true")
        slskd("POST", f"/transfers/downloads/{urllib.parse.quote(user)}", [{"filename": f["filename"], "size": f["size"]}])
        with lock:                      # a playlist song that failed goes back to "queued" on the same file
            for pl in st["playlists"].values():
                for t in pl["tracks"].values():
                    if t.get("user") == user and t.get("file") == f["filename"]:
                        t["status"] = "queued"
                        t["banned"] = [b for b in t.get("banned", []) if b != user]
            save(st)


def make_handler(st):
    pw = secret(r"password: (\S+)", SERVARR / "credentials.txt")
    expected = "Basic " + base64.b64encode(f"karuna:{pw}".encode()).decode()   # Homepage tile still uses this
    # browser sessions: cookie derived from the password → survives restarts, dies when the password changes
    session = hmac.new(pw.encode(), b"tunarr-session", hashlib.sha256).hexdigest()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def authed(self):
            cookie = re.search(r"(?:^|;\s*)tunarr=(\w+)", self.headers.get("Cookie", ""))
            # header login only for the Homepage tiles: browsers silently resend old popup credentials,
            # which would make Sign out a no-op everywhere else
            tile = urllib.parse.urlparse(self.path).path in ("/api/summary", "/api/backup")
            if (tile and hmac.compare_digest(self.headers.get("Authorization", ""), expected)) or (
                    cookie and hmac.compare_digest(cookie[1], session)):
                return True
            if self.path.startswith("/api/"):           # no WWW-Authenticate header → no browser popup
                self.reply({"ok": False, "error": "signed out"}, code=401)
            else:
                self.reply(LOGIN.replace("@@err@@", ""))
            return False

        def redirect(self, cookie, to="/"):
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Set-Cookie", f"tunarr={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if not cookie else 31536000}")
            self.end_headers()

        def reply(self, body, ctype="text/html; charset=utf-8", code=200, cache="no-store"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype if isinstance(body, str) else "application/json; charset=utf-8")
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/manifest.json":              # public: browsers fetch these without the login
                return self.reply(json.dumps(MANIFEST), "application/manifest+json", cache="max-age=86400")
            if u.path == "/icon.svg":
                return self.reply(ICON, "image/svg+xml", cache="max-age=86400")
            if u.path == "/logout":
                return self.redirect("")
            if not self.authed():
                return
            qs = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/search":
                    return self.reply(poll_search(qs.get("sid", [""])[0]))
                if u.path == "/api/downloads":
                    return self.reply(downloads_view(st))
                if u.path == "/api/summary":            # for the Homepage dashboard tile
                    with lock:
                        tr = [t for pl in st["playlists"].values() for t in pl["tracks"].values()]
                        mode = "Download now" if st["override_until"] else ("Active" if active(st, now()) else "Scheduled")
                    dl = downloads_view(st)["items"]
                    return self.reply({"downloading": sum(x["kind"] in ("active", "waiting") for x in dl),
                                       "done": sum(t["status"] == "done" for t in tr),
                                       "notfound": sum(t["status"] == "notfound" for t in tr), "mode": mode})
                if u.path == "/api/backup":             # Homepage "Backup" tile; written by ../backup.sh
                    f = SERVARR / "backup-status.json"
                    return self.reply(json.loads(f.read_text()) if f.exists() else {"status": "Never run", "last": "—"})
            except Exception as ex:
                return self.reply({"error": str(ex)[:200], "items": [], "groups": [], "complete": True, "responses": 0}, code=502)
            self.reply(render(st, self.headers.get("Host", "localhost")))

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if self.path.split("?")[0] == "/login":
                f = urllib.parse.parse_qs(raw)
                if hmac.compare_digest(f.get("user", [""])[0].strip().lower(), "karuna") & hmac.compare_digest(f.get("pw", [""])[0], pw):
                    q = urllib.parse.urlparse(self.path).query    # e.g. ?q=… from Discover's "+ Tunarr"
                    return self.redirect(session, "/?" + q + "#search" if q else "/")
                time.sleep(1)                           # slow down password guessing
                return self.reply(LOGIN.replace("@@err@@", '<div class="err">Wrong username or password</div>'), code=401)
            if not self.authed():
                return
            if self.path.startswith("/api/"):
                try:
                    d = json.loads(raw or "{}")
                    if self.path == "/api/search":
                        return self.reply({"sid": start_search(d["q"].strip())})
                    if self.path == "/api/dl":
                        slskd("POST", f"/transfers/downloads/{urllib.parse.quote(d['user'])}",
                              [{"filename": f["filename"], "size": int(f["size"])} for f in d["files"]])
                        return self.reply({"ok": True})
                    if self.path == "/api/transfer":
                        transfer_action(st, d["action"], d.get("user"), d.get("id"))
                        return self.reply({"ok": True})
                except Exception as ex:
                    return self.reply({"ok": False, "error": str(ex)[:200]}, code=502)
                return self.reply({"ok": False}, code=404)
            f = urllib.parse.parse_qs(raw)
            a, url = f.get("a", [""])[0], f.get("url", [""])[0].strip()
            song = None
            if a == "add" and video_id(url):
                try:
                    song = single(url)          # network call, done outside the lock
                except Exception:
                    pass
            with lock:
                if a == "add" and re.match(r"https://(music\.|www\.|m\.)?youtube\.com/playlist\?list=", url):
                    st["playlists"].setdefault(url, {"name": None, "tracks": {}})
                    sync.set()
                    wake.set()
                elif song:
                    pl = st["playlists"].setdefault(SINGLES, {"name": "Singles", "tracks": {}})
                    pl["tracks"].setdefault(song[0], {**song[1], "status": "pending", "tries": 0})
                    wake.set()
                elif a == "rmsong" and SINGLES in st["playlists"]:
                    st["playlists"][SINGLES]["tracks"].pop(f.get("vid", [""])[0], None)
                elif a == "remove":
                    st["playlists"].pop(url, None)
                elif a == "sync":
                    sync.set()
                    wake.set()
                elif a == "on":
                    st["override_until"] = next_end(now()).isoformat(timespec="seconds")
                    wake.set()
                elif a == "off":
                    st["override_until"] = None
                elif a == "retry":
                    for pl in st["playlists"].values():
                        for x in pl["tracks"].values():
                            if x["status"] == "notfound":
                                x.update(status="pending", last=None)
                    wake.set()
                save(st)
            self.send_response(303)
            self.send_header("Location", "/#playlists")
            self.end_headers()

    return H


def test():
    assert segments("Saregama Tamil", "June Ponal July Katre Song Lyrics | Unnale Unnale | Harris Jayaraj", True)[:2] == \
        ["June Ponal July Katre", "Unnale Unnale"]
    assert queries({"artist": "Dharmadurai", "title": "Makka Kalanguthappa Video Song | Vijay Sethupathi"}) == \
        ["Dharmadurai Makka Kalanguthappa", "Makka Kalanguthappa"]
    assert queries({"artist": "Saregama Tamil", "title": "Endi Ippadi | Enakkul Oruvan", "artist_wrong": True}) == \
        ["Endi Ippadi Enakkul Oruvan", "Endi Ippadi"]
    R = lambda u, files, slot=True, q=0, sp=100: {"username": u, "files": files, "hasFreeUploadSlot": slot, "queueLength": q, "uploadSpeed": sp}
    F = lambda n, size, br=None, locked=False: {"filename": n, "size": size, "bitRate": br, "isLocked": locked, "extension": ""}
    L = 240
    res = [R("a", [F("x\\song.mp3", 9_600_000, 320)]),
           R("b", [F("x\\song.mp3", 5_000_000, 192)]),                 # low bitrate
           R("c", [F("x\\song.flac", 30_000_000)], slot=False),         # flac wins even without slot
           R("d", [F("x\\song remix.flac", 30_000_000)]),               # remix filtered
           R("e", [F("x\\song.flac", 900_000_000)])]                    # size doesn't fit length
    assert pick(res, L, "song")["user"] == "c"
    assert pick(res, L, "song", banned=("c",))["user"] == "a"
    assert pick([R("b", [F("x\\s.mp3", 5_000_000, 192)])], L, "s") is None
    # the two real false matches: song name must be in the file name, not just the folder path
    maalai = {"artist": "Maalai Pozhudhin Mayakathilaey", "title": "Oh Baby Girl Video | Aari, Shubha | Achu"}
    kpop = [R("k", [F("music\\OH MY GIRL\\[2015] CLOSER\\OH MY GIRL - CLOSER - 04 - SUGAR BABY.flac", 30_000_000)])]
    assert pick(kpop, 195, "Oh Baby Girl", names=song_names(maalai)) is None
    loosu = {"artist": "Loosu Penne", "title": "Bass Boosted | Slingshot Music"}
    sling = [R("z", [F("music\\Late Arcane\\emergen-T (2020)\\07 - slingshot.flac", 40_000_000)])]
    assert pick(sling, 396, "Slingshot Music", names=song_names(loosu)) is None
    good = [R("g", [F("Tamil\\Maalai Pozhudhin Mayakathilaey\\02 - Oh Baby Girl.mp3", 7_800_000, 320)])]
    assert pick(good, 195, "Oh Baby Girl", names=song_names(maalai), context=_segs(maalai))["user"] == "g"
    pedro = [R("p", [F("music\\Pedro Floriani\\05-pedro_floriani-oh_baby_girl_i_have_to_go_(original_mix).flac", 30_000_000)])]
    assert pick(pedro, 195, "Oh Baby Girl", names=song_names(maalai), context=_segs(maalai)) is None
    solo = {"artist": "x", "title": "Endi Ippadi", "artist_wrong": True}   # no other clue -> name alone is enough
    assert pick([R("s", [F("a\\Endi Ippadi.mp3", 8_000_000, 320)])], 214, "Endi Ippadi",
                names=song_names(solo), context=_segs(solo))["user"] == "s"
    t = datetime.datetime(2026, 10, 23, 14, 0)
    assert next_end(t) == datetime.datetime(2026, 10, 24, 9, 0)
    assert next_end(t.replace(hour=3)) == datetime.datetime(2026, 10, 23, 9, 0)
    st = {"override_until": None}
    assert not active(st, t) and active(st, t.replace(hour=23)) and active(st, t.replace(hour=8))
    st["override_until"] = next_end(t).isoformat()
    assert active(st, t) and not active(st, datetime.datetime(2026, 10, 24, 10, 0)) and st["override_until"] is None
    page = render({"override_until": None, "playlists": {"https://x": {"name": "P<1>", "tracks": {
        "v": {"artist": "A", "title": "T", "status": "notfound", "tries": 2}}}}}, "10.0.0.5:5040")
    assert "P&lt;1&gt;" in page and "Not found ×2" in page and "http://10.0.0.5:5030" in page and "@@" not in page
    page = render({"override_until": "2026-10-24T09:00:00", "playlists": {SINGLES: {"name": "Singles", "tracks": {
        "abcdefghijk": {"artist": "A", "title": "T", "status": "queued", "tries": 0}}}}}, "h")
    assert "value='rmsong'" in page and "DOWNLOAD NOW" in page and "Downloading" in page and "@@" not in page
    assert video_id("https://music.youtube.com/watch?v=OxDKZ6WfD7M&si=x") == "OxDKZ6WfD7M"
    assert video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/watch?list=PLx&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://music.youtube.com/playlist?list=PLo3d") is None
    assert song_from_oembed("Kaathalae Kaathalae", "Govind Vasantha - Topic") == \
        {"artist": "Govind Vasantha", "title": "Kaathalae Kaathalae", "artist_wrong": False}
    assert song_from_oembed("Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley")["title"] == \
        "Never Gonna Give You Up (Official Video)"
    assert song_from_oembed("Makka Kalanguthappa | Dharmadurai", "TrendMusic")["artist_wrong"]
    resp = [R("u1", [F("Music\\OST\\01 a.mp3", 9_000_000, 320), F("Music\\OST\\02 b.mp3", 5_000_000, 192),
                     F("Music\\OST\\03 c.m4a", 9_000_000), F("Music\\OST\\04 d.flac", 30_000_000, locked=True)], slot=False),
            R("u2", [F("Lossless\\Album\\01 a.flac", 30_000_000)])]
    g = hd_groups(resp)
    assert [x["user"] for x in g] == ["u2", "u1"]                        # FLAC folder first
    assert [x["name"] for x in g[1]["files"]] == ["01 a.mp3"]            # 192k, m4a, locked dropped
    json.dumps(g)                                                        # API payload must serialise
    assert dl_kind("Completed, Succeeded") == ("ok", "Done")
    assert dl_kind("Completed, Errored") == ("failed", "Errored")
    assert dl_kind("Completed, Cancelled") == ("failed", "Cancelled")
    assert dl_kind("InProgress") == ("active", "Downloading")
    assert dl_kind("Queued, Remotely") == ("waiting", "Queued")
    page = render({"override_until": None, "playlists": {"u": {"name": "P", "tracks": {
        "v": {"artist": "Dharmadurai", "title": "Makka Kalanguthappa", "status": "notfound", "tries": 1}}}}}, "h")
    assert 'id="p-search"' in page and 'id="p-playlists"' in page and 'id="p-downloads"' in page and "@@" not in page
    assert "data-search='Makka Kalanguthappa'" in page and "fonts.googleapis" not in page and 'value="sync"' in page
    assert json.loads(json.dumps(MANIFEST))["display"] == "standalone" and ICON.startswith("<svg")
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        test()
        sys.exit()
    st = load()
    save(st)
    threading.Thread(target=worker, args=(st,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(st)).serve_forever()
