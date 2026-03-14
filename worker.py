#!/usr/bin/env python3
"""
worker.py — Distributed processing worker for the traffic analyser.

Runs on any machine (NAS, Mac, PC) that has Python + ultralytics installed.
Polls the NAS job server for unprocessed files, downloads them, runs YOLO
analysis locally, and posts results back to the NAS for DB insertion.

Usage:
    python worker.py --server http://192.168.1.99:5002
    python worker.py --server http://192.168.1.99:5002 --workers 2
    python worker.py --server http://192.168.1.99:5002 --dry-run

Requirements:
    pip install requests ultralytics opencv-python-headless

The worker needs no access to the NAS filesystem — everything goes over HTTP.
"""

import argparse
import gc
import json
import logging
import os
import platform
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers = [logging.StreamHandler(sys.stdout)],
)
for noisy in ("ultralytics", "torch", "PIL"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# ── Worker identity ───────────────────────────────────────────────────────────

WORKER_ID = f"{socket.gethostname()}_{platform.system()}"


# ── Args ──────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(description="Traffic analyser distributed worker")
    p.add_argument("--server",  required=True,
                   help="NAS server URL e.g. http://192.168.1.99:5002")
    p.add_argument("--poll",    type=int, default=15,
                   help="Seconds between polls when no work available (default: 15)")
    p.add_argument("--day",     action="store_true", help="Force day mode")
    p.add_argument("--night",   action="store_true", help="Force night mode")
    p.add_argument("--dry-run", action="store_true",
                   help="Claim and download files but don't process or post results")
    p.add_argument("--once",    action="store_true",
                   help="Process one file then exit")
    p.add_argument("--timeout", type=int, default=300,
                   help="Per-file processing timeout in seconds (default: 300)")
    return p.parse_args()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(server, path, **kwargs):
    try:
        r = requests.get(f"{server}{path}", timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"GET {path} failed: {e}")
        return None


def api_post(server, path, data=None, **kwargs):
    try:
        r = requests.post(f"{server}{path}", json=data, timeout=60, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"POST {path} failed: {e}")
        return None


def download_file(server, remote_path, local_path):
    """
    Download a video file from the NAS to a local temp path.
    Streams the download so large files don't require all memory at once.
    Returns True on success.
    """
    url = f"{server}/api/video?path={requests.utils.quote(remote_path)}"
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
            if total > 0:
                log.info(f"  Downloaded {downloaded/1024/1024:.1f} MB")
            return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        return False


# ── Analysis ──────────────────────────────────────────────────────────────────

def run_analyse(local_path, force_day=False, force_night=False):
    """
    Run analyse() on the local file.
    Returns list of vehicle dicts, or raises on error.

    We import analyse here rather than at module level so the worker
    can run on machines that have the project checked out, falling back
    to a minimal inline import if not.
    """
    try:
        # Try to import from the local project if worker.py is in the project dir
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from analyse import analyse
        vehicles = analyse(
            input_path  = local_path,
            force_day   = force_day,
            force_night = force_night,
            show        = False,
            save_db     = False,   # worker never writes to DB directly
            force       = True,    # always process — server has already decided
        )
        return vehicles or []
    except ImportError:
        raise RuntimeError(
            "Could not import analyse.py — make sure worker.py is in the "
            "traffic_analyser project directory."
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    args   = get_args()
    server = args.server.rstrip("/")

    log.info(f"Worker starting — ID: {WORKER_ID}")
    log.info(f"Server: {server}")
    if args.dry_run:
        log.info("DRY RUN mode — will not process or post results")

    # Verify server is reachable
    status = api_get(server, "/api/status")
    if status is None:
        log.error("Cannot reach server. Is web_ui.py running?")
        sys.exit(1)
    log.info(f"Server reachable — status: {status}")

    # Run DB migration to ensure retry columns exist
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import database as db
        db.migrate_job_locks()
        log.info("DB migration check complete")
    except Exception as e:
        log.warning(f"DB migration skipped (no local DB access): {e}")

    tmp_dir = tempfile.mkdtemp(prefix="traffic_worker_")
    log.info(f"Temp dir: {tmp_dir}")

    jobs_done = 0

    try:
        while True:
            # ── Claim a job ───────────────────────────────────────────────────
            job = api_post(server, "/api/jobs/next", {
                "worker_id": WORKER_ID,
            })

            if job is None:
                log.warning("Failed to contact job server, retrying...")
                time.sleep(args.poll)
                continue

            if job.get("empty"):
                log.info(f"No jobs available — waiting {args.poll}s...")
                time.sleep(args.poll)
                continue

            job_id      = job["job_id"]
            remote_path = job["path"]
            rel_path    = job.get("rel_path", remote_path)

            log.info(f"Claimed job {job_id}: {rel_path}")

            if args.dry_run:
                log.info("  DRY RUN — releasing job without processing")
                api_post(server, "/api/jobs/fail", {
                    "job_id":    job_id,
                    "worker_id": WORKER_ID,
                    "reason":    "dry_run",
                })
                if args.once:
                    break
                continue

            # ── Download ──────────────────────────────────────────────────────
            filename   = Path(remote_path).name
            local_path = os.path.join(tmp_dir, f"{job_id}_{filename}")

            log.info(f"  Downloading...")
            t_dl = time.time()
            ok   = download_file(server, remote_path, local_path)

            if not ok:
                log.error("  Download failed — releasing job")
                api_post(server, "/api/jobs/fail", {
                    "job_id":    job_id,
                    "worker_id": WORKER_ID,
                    "reason":    "download_failed",
                })
                continue

            dl_time = time.time() - t_dl
            size_mb = os.path.getsize(local_path) / 1024 / 1024
            log.info(f"  Downloaded {size_mb:.1f} MB in {dl_time:.1f}s")

            # ── Process ───────────────────────────────────────────────────────
            log.info(f"  Processing...")
            t_proc = time.time()

            try:
                vehicles = run_analyse(
                    local_path,
                    force_day   = args.day,
                    force_night = args.night,
                )
                proc_time = time.time() - t_proc
                log.info(f"  Processed in {proc_time:.1f}s — {len(vehicles)} vehicle(s)")

            except Exception as e:
                err = str(e).lower()
                retryable = any(x in err for x in (
                    'moov atom', 'invalid data', 'could not read',
                    'cannot open', 'end of file', 'truncated',
                ))
                log.error(f"  Processing failed ({'will retry' if retryable else 'permanent'}): {e}")
                traceback.print_exc()
                api_post(server, "/api/jobs/fail", {
                    "job_id":    job_id,
                    "worker_id": WORKER_ID,
                    "reason":    str(e)[:500],
                    "retryable": retryable,
                })
                _cleanup(local_path)
                continue

            # ── Post results ──────────────────────────────────────────────────
            log.info(f"  Posting results to server...")
            result = api_post(server, "/api/jobs/complete", {
                "job_id":    job_id,
                "worker_id": WORKER_ID,
                "vehicles":  vehicles,
            })

            if result and result.get("ok"):
                log.info(f"  Results accepted — recording_id={result.get('recording_id')}")
                jobs_done += 1
            else:
                log.error(f"  Server rejected results: {result}")

            _cleanup(local_path)
            gc.collect()

            if args.once:
                log.info("--once: exiting after one job")
                break

    except KeyboardInterrupt:
        log.info("Interrupted — exiting cleanly")
    finally:
        # Clean up temp dir
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        log.info(f"Worker done — processed {jobs_done} job(s)")


def _cleanup(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
