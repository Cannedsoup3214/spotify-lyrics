"""
Spotify Lyrics Launcher — self-updating
Checks GitHub, downloads/updates lyrics.py, then runs it automatically.
"""

import sys, os, subprocess, urllib.request, time, shutil

# ── CONFIG ─────────────────────────────────────────────────────────────────────
VERSION_URL  = "https://raw.githubusercontent.com/Cannedsoup3214/spotify-lyrics/refs/heads/main/version.txt"
SCRIPT_URL   = "https://raw.githubusercontent.com/Cannedsoup3214/spotify-lyrics/refs/heads/main/lyrics.py"
LOCAL_VERSION = "1.0.0"
SCRIPT_NAME  = "lyrics.py"

# ── HELPERS ────────────────────────────────────────────────────────────────────
def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read().decode("utf-8").strip()
    except Exception:
        return None

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
        return True
    except Exception as e:
        print(f"  Download error: {e}")
        return False

def pip_install(*packages):
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "--quiet", *packages],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def ensure_dependencies():
    needed = {"spotipy": "spotipy", "syrics": "syrics",
              "colorama": "colorama", "PIL": "pillow"}
    missing = []
    for imp, pkg in needed.items():
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"  Installing: {', '.join(missing)} ...")
        pip_install(*missing)
        print("  Done.\n")
    else:
        print("  All dependencies installed.\n")

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    folder      = exe_dir()
    script_path = os.path.join(folder, SCRIPT_NAME)

    print("╔══════════════════════════════════╗")
    print("║      Spotify Lyrics Launcher     ║")
    print("╚══════════════════════════════════╝\n")

    # ── Check for update ──────────────────────────────────────────────────────
    print("  Checking for updates...")
    remote_version = fetch(VERSION_URL)

    if remote_version is None:
        print("  Could not reach GitHub — using existing version.\n")
    elif remote_version == LOCAL_VERSION and os.path.exists(script_path):
        print(f"  Up to date (v{LOCAL_VERSION})\n")
    else:
        if remote_version and remote_version != LOCAL_VERSION:
            print(f"  Update found: v{LOCAL_VERSION} → v{remote_version}")
        else:
            print(f"  Downloading lyrics.py...")

        tmp = script_path + ".tmp"
        ok  = download_file(SCRIPT_URL, tmp)
        if ok:
            os.replace(tmp, script_path)
            print(f"  Downloaded ✓\n")
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            if not os.path.exists(script_path):
                print("\n  ERROR: Could not download lyrics.py.")
                input("\n  Press Enter to exit...")
                sys.exit(1)
            print("  Using existing lyrics.py\n")

    # ── Dependencies ──────────────────────────────────────────────────────────
    print("  Checking dependencies...")
    ensure_dependencies()

    # ── Run lyrics.py ─────────────────────────────────────────────────────────
    print("  Launching Spotify Lyrics...\n")
    time.sleep(0.5)

    try:
        subprocess.run([sys.executable, script_path], cwd=folder)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\n  Error: {e}")
        input("\n  Press Enter to exit...")

if __name__ == "__main__":
    main()
