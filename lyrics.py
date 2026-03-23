"""
Spotify Lyrics Viewer — final clean version
- No lyric flicker: full frame written in one stdout call
- No art flicker: double-buffered image files, WT pointed at inactive one
- Working [A] toggle
- Instant art swap on song change
pip install syrics spotipy colorama pillow
"""

import time, os, sys, shutil, threading, re, io, json
import urllib.request, tempfile, msvcrt, copy
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from syrics.api import Spotify as SyricsSpotify
from colorama import Style, init
from PIL import Image, ImageFilter

init()

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = "84201d04aad04dd0a92719d619295912"
SPOTIFY_CLIENT_SECRET = "ab373166d205453e8e9c5d8937b299f9"
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8888/callback"
SP_DC                 = "AQCf3tuj_zwUNMFySU90Di9pG0eHlNn8z52VPXGYWPs1dLuBHcdG9M5yCEAdwngBPEpijH4JexioZd-RoQb3RJwbM0ohFHOxZNyAhnfEvQ9wuP_krFhVyAX1KQMNMcc-vgk79LfbAaChrGHMS7foJQ0SU91Y5ECjmOqz5eWMSJ44erkn1fO8iKTg02kCbbvVWAkORK2HscNWpoVlyQ"

TEMP        = os.environ.get("TEMP", tempfile.gettempdir())
# Two image slots — we write to the inactive one, then switch
ART_SLOTS   = [
    os.path.join(TEMP, "sp_bg_a.jpg"),
    os.path.join(TEMP, "sp_bg_b.jpg"),
]
_active_slot = 0   # index of the slot WT is currently pointed at

# ── WINDOWS TERMINAL ───────────────────────────────────────────────────────────
def find_wt_settings():
    base = os.environ.get("LOCALAPPDATA", "")
    for pkg in ("Microsoft.WindowsTerminal_8wekyb3d8bbwe",
                "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"):
        p = os.path.join(base, "Packages", pkg, "LocalState", "settings.json")
        if os.path.exists(p): return p
    return None

WT_SETTINGS  = find_wt_settings()
_wt_lock     = threading.Lock()
_original_wt = None

def _load_wt():
    with open(WT_SETTINGS, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_wt(data):
    tmp = WT_SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, WT_SETTINGS)

def _patch_profiles(data, path, opacity):
    # Patch the defaults section — covers the Default profile and all profiles
    defaults = data.setdefault("profiles", {}).setdefault("defaults", {})
    if path:
        defaults["backgroundImage"]            = path.replace("\\", "/")
        defaults["backgroundImageOpacity"]     = opacity
        defaults["backgroundImageStretchMode"] = "uniformToFill"
        defaults["useAcrylic"]                 = False   # acrylic overrides bg image
    else:
        for k in ("backgroundImage","backgroundImageOpacity","backgroundImageStretchMode"):
            defaults.pop(k, None)
        defaults["useAcrylic"] = True   # restore acrylic when art is off

    # Also patch every profile in the list individually
    for p in data.get("profiles", {}).get("list", []):
        if path:
            p["backgroundImage"]            = path.replace("\\", "/")
            p["backgroundImageOpacity"]     = opacity
            p["backgroundImageStretchMode"] = "uniformToFill"
            p["useAcrylic"]                 = False
        else:
            for k in ("backgroundImage","backgroundImageOpacity","backgroundImageStretchMode"):
                p.pop(k, None)
            p["useAcrylic"] = True

def wt_set(path, opacity):
    if not WT_SETTINGS: return
    with _wt_lock:
        try:
            data = _load_wt()
            _patch_profiles(data, path, opacity)
            _save_wt(data)
        except Exception as e:
            # Write error to a log file so we can see what went wrong
            try:
                with open(os.path.join(TEMP, "sp_wt_error.txt"), "w") as f:
                    import traceback
                    f.write(f"wt_set error: {e}\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass

def wt_setup():
    global _original_wt
    if not WT_SETTINGS: return
    with _wt_lock:
        try:
            _original_wt = copy.deepcopy(_load_wt())
        except Exception: pass
    # Start hidden — no flash before first art loads
    wt_set(ART_SLOTS[0], 0.0)

def wt_restore():
    if not WT_SETTINGS or _original_wt is None: return
    with _wt_lock:
        try:
            _save_wt(copy.deepcopy(_original_wt))
        except Exception: pass
def process_art(url: str, out_path: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            img = Image.open(io.BytesIO(r.read())).convert("RGB")
        img  = img.resize((960, 960), Image.LANCZOS)
        img  = img.filter(ImageFilter.GaussianBlur(radius=22))
        dark = Image.new("RGB", img.size, (0, 0, 0))
        img  = Image.blend(img, dark, alpha=0.50)
        tmp  = out_path + ".tmp"
        img.save(tmp, "JPEG", quality=90)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False

# ── ANSI ───────────────────────────────────────────────────────────────────────
HIDE  = "\033[?25l"
SHOW  = "\033[?25h"
HOME  = "\033[H"
RESET = Style.RESET_ALL

def rgb(r, g, b): return f"\033[38;2;{r};{g};{b}m"
def grey(n):      return rgb(n, n, n)

FPS       = 30
FADE_SECS = 0.4

# ── SHARED STATE ───────────────────────────────────────────────────────────────
_lock = threading.Lock()
_s = dict(
    lyrics=[], label="", artist="", art_url="",
    ref_progress=0, ref_mono=0.0, duration_ms=1,
    playing=False, loading=False, status_msg="Starting…",
)
_cf = dict(cur_idx=-1, prev_idx=-1, fade_t=1.0, fade_mono=0.0)

_art_lock  = threading.Lock()
_art_on    = True
_art_ready = False

# ── HELPERS ────────────────────────────────────────────────────────────────────
def center(text, w):
    plain = re.sub(r'\033\[[^m]*m', '', text).rstrip()
    return " " * max(0, (w - len(plain)) // 2) + text

def live_progress():
    with _lock:
        if not _s["playing"]: return 0
        return min(_s["ref_progress"] + int((time.monotonic()-_s["ref_mono"])*1000), _s["duration_ms"])

def line_idx(lyrics, ms):
    if not lyrics: return -1
    lo, hi, res = 0, len(lyrics)-1, 0
    while lo <= hi:
        mid = (lo+hi)//2
        if lyrics[mid][0] <= ms: res=mid; lo=mid+1
        else: hi=mid-1
    return res

def ease_io(t): return t*t*(3-2*t)

def parse_lyrics(raw):
    if not raw: return []
    if raw.get("lyrics",{}).get("syncType") != "LINE_SYNCED": return []
    out = []
    for ln in raw.get("lyrics",{}).get("lines",[]):
        ms=int(ln.get("startTimeMs",0)); text=ln.get("words","").strip()
        if text and text != "♪": out.append((ms, text))
    return out

def trunc(text, w): return text[:w-1]+"…" if len(text)>w else text

# ── INPUT ──────────────────────────────────────────────────────────────────────
def input_loop():
    global _art_on
    while True:
        if msvcrt.kbhit():
            if msvcrt.getch().lower() == b'a':
                with _art_lock:
                    _art_on  = not _art_on
                    on       = _art_on
                    ready    = _art_ready
                if ready:
                    wt_set(ART_SLOTS[_active_slot], 1.0 if on else 0.0)
        time.sleep(0.05)

# ── ART MANAGER ────────────────────────────────────────────────────────────────
def art_loop():
    global _art_ready, _active_slot
    last_url = ""

    while True:
        time.sleep(0.5)

        with _lock:
            url     = _s["art_url"]
            playing = _s["playing"]
        with _art_lock:
            on = _art_on

        if not url or not playing or url == last_url:
            continue

        # Write to the INACTIVE slot so WT never reads a half-written file
        next_slot = 1 - _active_slot
        ok = process_art(url, ART_SLOTS[next_slot])
        last_url = url

        if ok:
            # Point WT at the newly written slot (one settings.json write per song)
            wt_set(ART_SLOTS[next_slot], 1.0 if on else 0.0)
            _active_slot = next_slot
            with _art_lock:
                _art_ready = True

# ── RENDER ─────────────────────────────────────────────────────────────────────
def render_loop():
    sys.stdout.write(HIDE)
    # Move to home and clear once — never cls again
    sys.stdout.write("\033[2J" + HOME)
    sys.stdout.flush()
    prev_line_count = 0

    while True:
        t0 = time.monotonic()

        with _lock:
            lyrics  = _s["lyrics"][:]
            label   = _s["label"]
            artist  = _s["artist"]
            dur     = _s["duration_ms"]
            playing = _s["playing"]
            loading = _s["loading"]
            status  = _s["status_msg"]
        with _art_lock:
            art_on = _art_on

        w, h = shutil.get_terminal_size((110, 40))
        prog  = live_progress()
        ridx  = line_idx(lyrics, prog)
        now   = time.monotonic()

        # ── crossfade ────────────────────────────────────────────────────────
        if ridx != _cf["cur_idx"] and ridx >= 0:
            _cf["prev_idx"]  = _cf["cur_idx"]
            _cf["cur_idx"]   = ridx
            _cf["fade_mono"] = now
            _cf["fade_t"]    = 0.0
        if _cf["fade_t"] < 1.0:
            _cf["fade_t"] = min((now - _cf["fade_mono"]) / FADE_SECS, 1.0)

        t   = ease_io(_cf["fade_t"])
        cur = _cf["cur_idx"]
        prv = _cf["prev_idx"]
        cb  = int(60 + 195 * t)
        pb  = int(255 * (1.0 - t))

        # ── build rows ───────────────────────────────────────────────────────
        rows = []

        if not playing or loading:
            for _ in range(h//2 - 1): rows.append("")
            rows.append(center(f"{grey(160)}{status}{RESET}", w))
        else:
            block_h = 7
            top_pad = max(2, (h - block_h) // 2 - 1)
            for _ in range(top_pad): rows.append("")

            rows.append(center(f"{grey(230)}{label}{RESET}", w))
            rows.append(center(f"{grey(150)}{artist}{RESET}", w))
            rows.append("")

            if not lyrics:
                rows.append("")
                rows.append(center(f"{grey(130)}Cannot retrieve lyrics{RESET}", w))
            else:
                rows.append(
                    center(f"{grey(pb)}{trunc(lyrics[prv][1], w-4)}{RESET}", w)
                    if prv >= 0 and prv < len(lyrics) and pb > 5 else ""
                )
                rows.append(
                    center(f"\033[1m{grey(cb)}{trunc(lyrics[cur][1], w-4)}{RESET}", w)
                    if cur >= 0 and cur < len(lyrics) else ""
                )

            rows.append("")
            es, ts = prog//1000, dur//1000
            bw     = min(42, w - 18)
            filled = int(bw * prog / dur) if dur else 0
            bar    = f"{rgb(130,90,210)}{'█'*filled}{RESET}{grey(40)}{'░'*(bw-filled)}{RESET}"
            rows.append(center(
                f"{grey(120)}{es//60}:{es%60:02d}{RESET}  {bar}  {grey(120)}{ts//60}:{ts%60:02d}{RESET}", w
            ))

        # pin button to bottom-right
        while len(rows) < h - 1: rows.append("")
        btn   = f"{rgb(130,90,210)}[A] ■ Album Art{RESET}" if art_on else f"{grey(70)}[A] □ Album Art{RESET}"
        plain = re.sub(r'\033\[[^m]*m', '', btn)
        rows.append(" " * max(0, w - len(plain) - 2) + btn + "  ")

        # ── single atomic write — no per-line clear, no flash ────────────────
        frame = HOME
        for row in rows:
            # pad each line to full width so leftover chars from prev frame are overwritten
            plain_row = re.sub(r'\033\[[^m]*m', '', row)
            padding   = max(0, w - len(plain_row))
            frame    += row + " " * padding + "\n"
        # blank any lines from a taller previous frame
        for _ in range(max(0, prev_line_count - len(rows))):
            frame += " " * w + "\n"

        sys.stdout.write(frame)
        sys.stdout.flush()
        prev_line_count = len(rows)

        spent = time.monotonic() - t0
        time.sleep(max(0.0, 1/FPS - spent))

# ── SPOTIFY POLL ───────────────────────────────────────────────────────────────
def spotify_loop():
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-currently-playing",
    ))

    with _lock: _s["status_msg"] = "Connecting…"
    try:
        sy = SyricsSpotify(SP_DC)
    except Exception as e:
        with _lock: _s["status_msg"] = f"Auth failed: {e}"
        return

    last_id = ""

    while True:
        try:
            cur = sp.current_user_playing_track()
        except Exception:
            time.sleep(5); continue

        if not cur or not cur.get("is_playing"):
            with _lock:
                _s["playing"]    = False
                _s["status_msg"] = "⏸  Nothing playing"
            time.sleep(4); continue

        item        = cur["item"]
        track_id    = item["id"]
        name        = item["name"]
        artist_name = item["artists"][0]["name"]
        duration_ms = item["duration_ms"]
        progress_ms = cur.get("progress_ms", 0)
        poll_mono   = time.monotonic()
        images      = item.get("album", {}).get("images", [])
        art_url     = images[0]["url"] if images else ""

        with _lock:
            _s["ref_progress"] = progress_ms
            _s["ref_mono"]     = poll_mono
            _s["duration_ms"]  = duration_ms
            _s["playing"]      = True
            _s["label"]        = name
            _s["artist"]       = artist_name
            _s["art_url"]      = art_url

        if track_id != last_id:
            last_id = track_id
            _cf["cur_idx"] = _cf["prev_idx"] = -1
            _cf["fade_t"]  = 1.0
            with _lock:
                _s["loading"]    = True
                _s["lyrics"]     = []
                _s["status_msg"] = "Loading…"
            try:
                raw    = sy.get_lyrics(track_id)
                lyrics = parse_lyrics(raw)
                msg    = "" if lyrics else "Cannot retrieve lyrics"
            except Exception:
                lyrics = []
                msg    = "Cannot retrieve lyrics"
            with _lock:
                _s["lyrics"]     = lyrics
                _s["loading"]    = False
                _s["status_msg"] = msg

        time.sleep(3)

# ── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    wt_setup()
    for fn in (render_loop, spotify_loop, input_loop, art_loop):
        threading.Thread(target=fn, daemon=True).start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        wt_restore()
        sys.stdout.write(SHOW + RESET + "\n")
