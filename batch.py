#!/usr/bin/env python3
"""
batch.py — Process all unprocessed traffic recordings.

Walks the recordings folder, skips any already in the database,
and processes the rest in chronological order.

Usage:
    python batch.py                        # process all unprocessed
    python batch.py --dry-run              # show what would be processed
    python batch.py --force                # reprocess everything
    python batch.py --limit 10             # process at most N files
    python batch.py --camera Camera1       # limit to one camera
    python batch.py --since 2026-03-01     # only files on or after this date
"""

import argparse
import os
import sys
import traceback
from datetime import datetime, date

from config import RECORDINGS_ROOT, DB_USER, DB_PASSWORD

# ── Args ──────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(description="Batch process traffic recordings")
    p.add_argument("--dry-run",  action="store_true",
                   help="Show what would be processed without doing it")
    p.add_argument("--force",    action="store_true",
                   help="Reprocess files already in the database")
    p.add_argument("--limit",    type=int, default=None,
                   help="Maximum number of files to process")
    p.add_argument("--camera",   default=None,
                   help="Only process this camera folder (e.g. Camera1)")
    p.add_argument("--since",    default=None,
                   help="Only process recordings on or after YYYY-MM-DD")
    p.add_argument("--day",      action="store_true", help="Force day mode")
    p.add_argument("--night",    action="store_true", help="Force night mode")
    return p.parse_args()


# ── File discovery ────────────────────────────────────────────────────────────

def find_recordings(root, camera=None, since=None):
    """
    Walk recordings folder and return sorted list of .mp4 paths.
    Structure: root/CameraX/YYYY-MM-DD/HH-MM-SS.mp4
    """
    since_date = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            print(f"ERROR: --since date must be YYYY-MM-DD, got: {since}")
            sys.exit(1)

    recordings = []

    camera_dirs = [camera] if camera else os.listdir(root)
    for cam in sorted(camera_dirs):
        cam_path = os.path.join(root, cam)
        if not os.path.isdir(cam_path):
            continue

        for date_dir in sorted(os.listdir(cam_path)):
            date_path = os.path.join(cam_path, date_dir)
            if not os.path.isdir(date_path):
                continue

            # Filter by date
            if since_date:
                try:
                    folder_date = date.fromisoformat(date_dir)
                    if folder_date < since_date:
                        continue
                except ValueError:
                    pass

            for fname in sorted(os.listdir(date_path)):
                if fname.lower().endswith(".mp4"):
                    recordings.append(os.path.join(date_path, fname))

    return recordings


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = get_args()

    import database as db
    from analyse import analyse

    recordings = find_recordings(
        RECORDINGS_ROOT,
        camera=args.camera,
        since=args.since,
    )

    if not recordings:
        print(f"No recordings found in {RECORDINGS_ROOT}")
        return

    # Filter out already-processed unless --force
    if not args.force:
        pending = []
        for path in recordings:
            if db.is_already_processed(os.path.abspath(path)):
                print(f"  SKIP (already processed): {path}")
            else:
                pending.append(path)
        recordings = pending
    else:
        print("--force: reprocessing all files")

    if not recordings:
        print("Nothing to process.")
        return

    # Apply limit
    if args.limit:
        recordings = recordings[:args.limit]

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}"
          f"Found {len(recordings)} file(s) to process\n")

    success = 0
    failed  = 0
    skipped = 0

    for i, path in enumerate(recordings, 1):
        rel = os.path.relpath(path, RECORDINGS_ROOT)
        print(f"[{i}/{len(recordings)}] {rel}")

        if args.dry_run:
            continue

        try:
            results = analyse(
                input_path  = path,
                force_night = args.night,
                force_day   = args.day,
                show        = False,
                save_db     = True,
                force       = args.force,
            )
            if results is None:
                # analyse() returned None = was skipped (already processed, no --force)
                skipped += 1
            else:
                print(f"  -> {len(results)} vehicle(s) recorded\n")
                success += 1
        except Exception as e:
            print(f"  ERROR processing {path}:")
            traceback.print_exc()
            failed += 1
            continue

    # Summary
    print(f"\n{'='*55}")
    if args.dry_run:
        print(f"Dry run complete — {len(recordings)} file(s) would be processed")
    else:
        print(f"Batch complete:")
        print(f"  Processed : {success}")
        print(f"  Skipped   : {skipped}")
        print(f"  Failed    : {failed}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
