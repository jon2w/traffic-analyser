#!/usr/bin/env python3
"""
verify.py — Download a recording from the NAS and run local YOLO annotation.

Usage:
    python verify.py --server http://traffic.justridingalong.com \
                     --path "/volume1/traffic/recordings/Camera1/2025-01-15/14-32-00.mp4"

    # Or short form (path copied from the busiest periods page):
    python verify.py -s http://traffic.justridingalong.com -p /volume1/.../14-32-00.mp4

Output:
    Downloads the raw .mp4 to the current directory, runs analyse.py with
    --no-show --output <name>_annotated.mp4, then opens the result.

Requirements (same as worker):
    pip install requests ultralytics opencv-python
    Python 3.11  (3.14 incompatible with PyTorch)

Notes:
    - analyse.py must be on the Python path or in the same directory.
    - Does NOT write to the database (no --save-db flag).
    - The annotated video is saved alongside the downloaded file.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import platform
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)


# ── Args ──────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(
        description="Download and locally annotate a traffic recording"
    )
    p.add_argument("-s", "--server", required=True,
                   help="Dashboard URL, e.g. http://traffic.justridingalong.com or http://192.168.1.99:5003")
    p.add_argument("-p", "--path", required=True,
                   help="Full NAS path to the recording (copy from Busiest Periods page)")
    p.add_argument("-o", "--output-dir", default=".",
                   help="Directory to save downloaded + annotated files (default: current dir)")
    p.add_argument("--day",   action="store_true", help="Force day mode")
    p.add_argument("--night", action="store_true", help="Force night mode")
    p.add_argument("--no-open", action="store_true",
                   help="Don't auto-open the annotated video when done")
    p.add_argument("--keep-raw", action="store_true",
                   help="Keep the downloaded raw file after annotating")
    p.add_argument("--analyse-script", default=None,
                   help="Path to analyse.py (default: auto-detect alongside this script)")
    return p.parse_args()


# ── Download ──────────────────────────────────────────────────────────────────

def download(server: str, nas_path: str, dest: Path) -> None:
    url    = f"{server.rstrip('/')}/api/download"
    params = {"path": nas_path}

    print(f"⬇  Downloading {Path(nas_path).name} …")
    with requests.get(url, params=params, stream=True, timeout=60) as r:
        if r.status_code == 403:
            print("ERROR: Server refused — path is outside the recordings folder.")
            sys.exit(1)
        if r.status_code == 404:
            print("ERROR: File not found on server.")
            sys.exit(1)
        r.raise_for_status()

        total = int(r.headers.get("content-length", 0))
        done  = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r   {pct:3d}%  {done/1_000_000:.1f} / {total/1_000_000:.1f} MB", end="", flush=True)
    print(f"\r   ✓ Saved to {dest}                          ")


# ── Annotate ─────────────────────────────────────────────────────────────────

def find_analyse_script(hint: str | None) -> Path:
    """Locate analyse.py — alongside this script, or in cwd."""
    if hint:
        p = Path(hint)
        if not p.exists():
            print(f"ERROR: analyse.py not found at {hint}")
            sys.exit(1)
        return p

    candidates = [
        Path(__file__).parent / "analyse.py",
        Path.cwd() / "analyse.py",
    ]
    for c in candidates:
        if c.exists():
            return c

    print("ERROR: Could not find analyse.py. Use --analyse-script to specify its path.")
    sys.exit(1)


def annotate(analyse_script: Path, input_path: Path, output_path: Path,
             day: bool, night: bool) -> None:
    cmd = [
        sys.executable, str(analyse_script),
        "--input",   str(input_path),
        "--output",  str(output_path),
        "--no-show",
    ]
    if day:   cmd.append("--day")
    if night: cmd.append("--night")

    print(f"\n🔍 Running analyser …  (this may take a minute)")
    print(f"   Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nERROR: analyse.py exited with code {result.returncode}")
        sys.exit(result.returncode)


# ── Open video ────────────────────────────────────────────────────────────────

def open_video(path: Path) -> None:
    system = platform.system()
    print(f"\n▶  Opening {path.name} …")
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = get_args()

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    basename    = Path(args.path).name
    stem        = Path(basename).stem
    raw_path    = out_dir / basename
    annot_path  = out_dir / f"{stem}_annotated.mp4"

    # 1. Download
    download(args.server, args.path, raw_path)

    # 2. Annotate
    analyse = find_analyse_script(args.analyse_script)
    annotate(analyse, raw_path, annot_path, args.day, args.night)

    # 3. Clean up raw file unless --keep-raw
    if not args.keep_raw and raw_path.exists():
        raw_path.unlink()
        print(f"   Removed raw file {raw_path.name}")

    # 4. Open result
    if annot_path.exists():
        print(f"\n✅ Done! Annotated video: {annot_path}")
        if not args.no_open:
            open_video(annot_path)
    else:
        print("\nWARNING: Annotated output not found — check analyse.py output above.")


if __name__ == "__main__":
    main()
