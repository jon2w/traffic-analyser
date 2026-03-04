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
    python batch.py --from-file failed.txt # reprocess a list of paths

Overnight run tips:
    nohup python batch.py --save-db 2>/dev/null &
    tail -f logs/batch_YYYY-MM-DD_HH-MM-SS.log
"""

import argparse
import gc
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, date, timedelta
from multiprocessing import Process, Queue

from config import RECORDINGS_ROOT

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def setup_logging():
    """
    Set up logging to both terminal (INFO) and a timestamped log file (DEBUG).
    Returns the path to the log file.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = datetime.now().strftime("batch_%Y-%m-%d_%H-%M-%S.log")
    log_path     = os.path.join(LOG_DIR, log_filename)

    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level   = logging.DEBUG,
        format  = fmt,
        datefmt = datefmt,
        handlers = [
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Suppress noisy third-party loggers
    for noisy in ("ultralytics", "torch", "PIL"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    return log_path


log = logging.getLogger(__name__)


# ── Args ──────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(description="Batch process traffic recordings")
    p.add_argument("--dry-run",   action="store_true",
                   help="Show what would be processed without doing it")
    p.add_argument("--force",     action="store_true",
                   help="Reprocess files already in the database")
    p.add_argument("--limit",     type=int, default=None,
                   help="Maximum number of files to process")
    p.add_argument("--camera",    default=None,
                   help="Only process this camera folder (e.g. Camera1)")
    p.add_argument("--since",     default=None,
                   help="Only process recordings on or after YYYY-MM-DD")
    p.add_argument("--day",       action="store_true", help="Force day mode")
    p.add_argument("--night",     action="store_true", help="Force night mode")
    p.add_argument("--from-file", default=None, metavar="FILE",
                   help="Read list of paths to process from a file (one per line)")
    p.add_argument("--timeout",   type=int, default=300,
                   help="Per-file timeout in seconds (default: 300)")
    p.add_argument("--failed-out", default="failed.txt", metavar="FILE",
                   help="Write failed file paths here (default: failed.txt)")
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
            log.error(f"--since date must be YYYY-MM-DD, got: {since}")
            sys.exit(1)

    recordings = []
    camera_dirs = [camera] if camera else sorted(os.listdir(root))

    for cam in camera_dirs:
        cam_path = os.path.join(root, cam)
        if not os.path.isdir(cam_path):
            continue

        for date_dir in sorted(os.listdir(cam_path)):
            date_path = os.path.join(cam_path, date_dir)
            if not os.path.isdir(date_path):
                continue

            if since_date:
                try:
                    if date.fromisoformat(date_dir) < since_date:
                        continue
                except ValueError:
                    pass

            for fname in sorted(os.listdir(date_path)):
                if fname.lower().endswith(".mp4"):
                    recordings.append(os.path.join(date_path, fname))

    return recordings


def load_from_file(path):
    """Load a list of recording paths from a text file (one per line)."""
    if not os.path.exists(path):
        log.error(f"--from-file path does not exist: {path}")
        sys.exit(1)
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    log.info(f"Loaded {len(lines)} path(s) from {path}")
    return lines


# ── Per-file timeout via subprocess ──────────────────────────────────────────

def _worker(path, force_night, force_day, force, result_q):
    """
    Run analyse() in a child process so we can kill it on timeout.
    Sends result count or exception back via queue.
    """
    try:
        from analyse import analyse
        results = analyse(
            input_path  = path,
            force_night = force_night,
            force_day   = force_day,
            show        = False,
            save_db     = True,
            force       = force,
        )
        result_q.put(("ok", len(results) if results else 0))
    except Exception as e:
        result_q.put(("err", traceback.format_exc()))


def process_with_timeout(path, force_night, force_day, force, timeout_s):
    """
    Run analyse() in a child process with a hard timeout.

    Returns (status, value) where status is:
      "ok"      — value is vehicle count (int)
      "skip"    — file was already processed (value is 0)
      "timeout" — process killed after timeout_s seconds
      "error"   — value is error string
    """
    q = Queue()
    p = Process(target=_worker, args=(path, force_night, force_day, force, q))
    p.start()
    p.join(timeout_s)

    if p.is_alive():
        p.terminate()
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join()
        return ("timeout", 0)

    if q.empty():
        return ("error", "Worker exited with no result (crash or OOM)")

    status, value = q.get_nowait()
    return (status, value)


# ── ETA tracker ───────────────────────────────────────────────────────────────

class ETATracker:
    def __init__(self, total):
        self.total      = total
        self.done       = 0
        self.start_time = time.time()
        self.times      = []   # rolling window of per-file durations

    def record(self, duration_s):
        self.done += 1
        self.times.append(duration_s)
        if len(self.times) > 50:
            self.times.pop(0)

    def eta_str(self):
        if not self.times or self.done == 0:
            return "ETA: unknown"
        remaining   = self.total - self.done
        avg_s       = sum(self.times) / len(self.times)
        eta_s       = remaining * avg_s
        eta_td      = timedelta(seconds=int(eta_s))
        elapsed_td  = timedelta(seconds=int(time.time() - self.start_time))
        pct         = 100.0 * self.done / self.total
        return (f"{pct:.1f}% | {self.done}/{self.total} | "
                f"avg {avg_s:.1f}s/file | "
                f"elapsed {elapsed_td} | ETA {eta_td}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log_path = setup_logging()
    args     = get_args()

    log.info(f"Batch started — log: {log_path}")
    log.info(f"Args: {vars(args)}")

    import database as db

    # ── Discover files ────────────────────────────────────────────────────────
    if args.from_file:
        recordings = load_from_file(args.from_file)
    else:
        recordings = find_recordings(
            RECORDINGS_ROOT,
            camera = args.camera,
            since  = args.since,
        )

    if not recordings:
        log.warning(f"No recordings found in {RECORDINGS_ROOT}")
        return

    log.info(f"Found {len(recordings)} recording(s) total")

    # ── Filter already-processed ──────────────────────────────────────────────
    if not args.force:
        pending = []
        for path in recordings:
            if db.is_already_processed(os.path.abspath(path)):
                log.debug(f"SKIP (done): {path}")
            else:
                pending.append(path)
        skipped_db = len(recordings) - len(pending)
        if skipped_db:
            log.info(f"Skipping {skipped_db} already-processed file(s)")
        recordings = pending
    else:
        log.info("--force: reprocessing all files")

    if not recordings:
        log.info("Nothing to process.")
        return

    if args.limit:
        recordings = recordings[:args.limit]
        log.info(f"--limit: processing first {args.limit} file(s)")

    log.info(f"\n{'DRY RUN — ' if args.dry_run else ''}"
             f"{len(recordings)} file(s) to process "
             f"(timeout {args.timeout}s/file)\n")

    # ── Batch loop ────────────────────────────────────────────────────────────
    eta     = ETATracker(len(recordings))
    success = 0
    failed  = 0
    skipped = 0
    failed_paths = []

    for i, path in enumerate(recordings, 1):
        rel = os.path.relpath(path, RECORDINGS_ROOT)
        log.info(f"[{i}/{len(recordings)}] {rel}")

        if args.dry_run:
            continue

        t0 = time.time()

        status, value = process_with_timeout(
            path        = path,
            force_night = args.night,
            force_day   = args.day,
            force       = args.force,
            timeout_s   = args.timeout,
        )

        duration = time.time() - t0

        if status == "ok":
            log.info(f"  -> {value} vehicle(s) | {duration:.1f}s")
            success += 1
        elif status == "skip":
            log.debug(f"  -> already processed")
            skipped += 1
        elif status == "timeout":
            log.error(f"  -> TIMEOUT after {args.timeout}s — skipping")
            failed += 1
            failed_paths.append(path)
        elif status == "error":
            log.error(f"  -> ERROR:\n{value}")
            failed += 1
            failed_paths.append(path)

        eta.record(duration)
        log.info(f"  {eta.eta_str()}")

        # Encourage Python to release memory between files
        gc.collect()

    # ── Write failed list ─────────────────────────────────────────────────────
    if failed_paths:
        with open(args.failed_out, "w") as f:
            f.write("# Failed recordings — reprocess with:\n")
            f.write(f"# python batch.py --from-file {args.failed_out}\n\n")
            for p in failed_paths:
                f.write(p + "\n")
        log.warning(f"Wrote {len(failed_paths)} failed path(s) to {args.failed_out}")

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = timedelta(seconds=int(time.time() - eta.start_time))
    log.info(f"\n{'='*55}")
    if args.dry_run:
        log.info(f"Dry run complete — {len(recordings)} file(s) would be processed")
    else:
        log.info(f"Batch complete in {elapsed}:")
        log.info(f"  Processed : {success}")
        log.info(f"  Skipped   : {skipped}")
        log.info(f"  Failed    : {failed}")
        if failed_paths:
            log.info(f"  Failed list: {args.failed_out}")
    log.info(f"{'='*55}\n")


if __name__ == "__main__":
    main()
