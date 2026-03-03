"""
monitor.py — Watch a folder for new recordings and auto-analyse them.

Runs continuously, checking every MONITOR_POLL_INTERVAL seconds.
Skips files already in the database. Waits for files to be at least
MONITOR_MIN_FILE_AGE seconds old before processing (ensures complete write).

Usage:
    python monitor.py                    # run continuously
    python monitor.py --once             # process any pending files and exit
    python monitor.py --dry-run          # show what would be processed
"""

import argparse
import os
import sys
import time
import glob
from datetime import datetime

from config import (
    RECORDINGS_ROOT, MONITOR_POLL_INTERVAL, MONITOR_MIN_FILE_AGE
)


def get_args():
    p = argparse.ArgumentParser(description="Traffic recording monitor")
    p.add_argument("--once",    action="store_true",
                   help="Process pending files and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be processed without doing it")
    p.add_argument("--no-show", action="store_true", default=True,
                   help="Don't show video windows (default for monitor)")
    p.add_argument("--save-db", action="store_true", default=True,
                   help="Save results to database (default on)")
    return p.parse_args()


def find_recordings(root):
    """Find all .mp4 files under the recordings root."""
    pattern = os.path.join(root, "**", "*.mp4")
    return sorted(glob.glob(pattern, recursive=True))


def is_old_enough(path):
    """Return True if file was last modified at least MIN_FILE_AGE seconds ago."""
    try:
        age = time.time() - os.path.getmtime(path)
        return age >= MONITOR_MIN_FILE_AGE
    except OSError:
        return False


def process_pending(dry_run=False, show=False, save_db=True):
    """Find and process any unprocessed recordings."""
    import database as db
    from analyse import analyse

    recordings = find_recordings(RECORDINGS_ROOT)
    if not recordings:
        print(f"No recordings found in {RECORDINGS_ROOT}")
        return 0

    pending = []
    for path in recordings:
        if not is_old_enough(path):
            continue
        if db.is_already_processed(path):
            continue
        pending.append(path)

    print(f"Found {len(pending)} pending recording(s) "
          f"(of {len(recordings)} total)")

    processed = 0
    for path in pending:
        print(f"\n{'='*55}")
        print(f"Processing: {path}")
        print(f"{'='*55}")

        if dry_run:
            print("  (dry run — skipping)")
            continue

        try:
            analyse(
                input_path  = path,
                output_path = None,
                show        = show,
                save_db     = save_db,
                save_thumbs = True,
            )
            processed += 1
        except Exception as e:
            print(f"ERROR processing {path}: {e}")
            import traceback
            traceback.print_exc()

    return processed


def run_monitor(args):
    print(f"Traffic monitor started")
    print(f"Watching: {RECORDINGS_ROOT}")
    print(f"Poll interval: {MONITOR_POLL_INTERVAL}s")
    print(f"Min file age: {MONITOR_MIN_FILE_AGE}s")
    print(f"Database: {'enabled' if args.save_db else 'disabled'}")
    print(f"Press Ctrl+C to stop\n")

    while True:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] Checking for new recordings...")
            n = process_pending(dry_run=args.dry_run,
                                show=not args.no_show,
                                save_db=args.save_db)
            if n:
                print(f"  Processed {n} recording(s)")
            else:
                print(f"  Nothing to do")

            if args.once:
                break

            time.sleep(MONITOR_POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"Monitor error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    args = get_args()
    run_monitor(args)
