#!/usr/bin/env python3
"""
web_ui.py — Browser-based interface for the traffic analyser.

Usage:
    python web_ui.py                  # starts on port 5000
    python web_ui.py --port 5001      # custom port

Access at: http://192.168.1.99:5000
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, date

from flask import (Flask, Response, jsonify, render_template_string,
                   request, send_file, stream_with_context)

from config import RECORDINGS_ROOT
import database as db

app = Flask(__name__)

# ── State ─────────────────────────────────────────────────────────────────────

# Currently running job (only one at a time)
_job_lock   = threading.Lock()
_job        = {
    "running":   False,
    "pid":       None,
    "log":       [],
    "type":      None,   # "analyse" or "batch"
    "started":   None,
    "output":    None,   # path to annotated video if produced
}

ANNOTATED_DIR = "/volume1/traffic/annotated"
os.makedirs(ANNOTATED_DIR, exist_ok=True)

VENV_PYTHON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../traffic_venv/bin/python"
)
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

ANALYSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyse.py")
BATCHER  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch.py")


# ── HTML Template ─────────────────────────────────────────────────────────────



# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/recordings")
def api_recordings():
    """Return file tree grouped by date."""
    tree = {}
    processed_set = set()

    # Get all processed filenames from DB
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filename FROM recordings WHERE processed_at IS NOT NULL")
            for (fn,) in cursor.fetchall():
                processed_set.add(fn)
    except Exception:
        pass

    if not os.path.isdir(RECORDINGS_ROOT):
        return jsonify({"tree": {}, "processed": []})

    for cam in sorted(os.listdir(RECORDINGS_ROOT)):
        cam_path = os.path.join(RECORDINGS_ROOT, cam)
        if not os.path.isdir(cam_path):
            continue
        for date_dir in sorted(os.listdir(cam_path), reverse=True):
            date_path = os.path.join(cam_path, date_dir)
            if not os.path.isdir(date_path):
                continue
            files = []
            for fname in sorted(os.listdir(date_path)):
                if not fname.lower().endswith(".mp4"):
                    continue
                full = os.path.join(date_path, fname)
                files.append({
                    "path":     full,
                    "label":    fname,
                    "is_night": False,
                })
            if files:
                key = f"{cam}/{date_dir}"
                tree[key] = files

    return jsonify({"tree": tree, "processed": list(processed_set)})


@app.route("/api/file_info")
def api_file_info():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"})

    import cv2
    cap = cv2.VideoCapture(path)
    fps    = cap.get(cv2.CAP_PROP_FPS) or 0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    w      = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    dur = frames / fps if fps > 0 else 0

    # Parse datetime from path
    basename = os.path.basename(path)
    parent   = os.path.basename(os.path.dirname(path))
    recorded_at = f"{parent} {basename.replace('.mp4','').replace('-',':')}"

    processed = db.is_already_processed(os.path.abspath(path))

    return jsonify({
        "recorded_at": recorded_at,
        "duration":    f"{dur:.1f}s",
        "resolution":  f"{w}×{h}",
        "fps":         f"{fps:.1f}",
        "processed":   processed,
    })


def _run_job(cmd, job_type, output_path=None):
    """Run a subprocess job, capturing output into _job['log']."""
    global _job
    with _job_lock:
        if _job["running"]:
            return False
        _job = {"running": True, "log": [], "type": job_type,
                "started": datetime.now(), "output": output_path, "pid": None}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        _job["pid"] = proc.pid
        for line in proc.stdout:
            _job["log"].append(("out", line.rstrip()))
        proc.wait()

        # Note: Synology ffmpeg lacks H.264 encoder — serve original for download
        if job_type == "analyse" and output_path and os.path.exists(output_path):
            _job["log"].append(("out", f"Annotated video saved: {output_path}"))

    except Exception as e:
        _job["log"].append(("err", str(e)))
    finally:
        _job["running"] = False


@app.route("/api/analyse", methods=["POST"])
def api_analyse():
    global _job
    if _job["running"]:
        return jsonify({"error": "A job is already running"})

    data = request.json
    path = data.get("path", "")
    mode = data.get("mode", "auto")

    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"})

    # Build output path
    basename   = os.path.splitext(os.path.basename(path))[0]
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_mp4 = os.path.join(ANNOTATED_DIR, f"{basename}_{ts}.mp4")

    cmd = [VENV_PYTHON, ANALYSER,
           "--input", path,
           "--output", output_mp4,
           "--no-show"]
    if mode == "day":   cmd.append("--day")
    if mode == "night": cmd.append("--night")

    threading.Thread(target=_run_job, args=(cmd, "analyse", output_mp4),
                     daemon=True).start()
    time.sleep(0.2)

    return jsonify({"stream_url": "/api/stream"})


@app.route("/api/batch", methods=["POST"])
def api_batch():
    global _job
    if _job["running"]:
        return jsonify({"error": "A job is already running"})

    data    = request.json
    cmd     = [VENV_PYTHON, BATCHER]
    mode    = data.get("mode", "auto")
    since   = data.get("since", "")
    camera  = data.get("camera", "")
    limit   = data.get("limit", "")
    dry_run = data.get("dry_run", False)
    force   = data.get("force", False)

    if since:   cmd += ["--since",  since]
    if camera:  cmd += ["--camera", camera]
    if limit:   cmd += ["--limit",  str(limit)]
    if mode == "day":   cmd.append("--day")
    if mode == "night": cmd.append("--night")
    if dry_run: cmd.append("--dry-run")
    if force:   cmd.append("--force")
    cmd.append("--save-db") if not dry_run else None

    threading.Thread(target=_run_job, args=(cmd, "batch"),
                     daemon=True).start()
    time.sleep(0.2)

    return jsonify({"stream_url": "/api/stream"})


@app.route("/api/stream")
def api_stream():
    """SSE stream of current job output."""
    def generate():
        sent = 0
        while True:
            log   = _job["log"]
            running = _job["running"]
            while sent < len(log):
                kind, line = log[sent]
                yield f"data: {json.dumps({'line': line, 'err': kind=='err'})}\n\n"
                sent += 1
            if not running and sent >= len(log):
                yield f"data: {json.dumps({'done': True})}\n\n"
                return
            time.sleep(0.1)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    import signal
    if _job["running"] and _job.get("pid"):
        try:
            os.kill(_job["pid"], signal.SIGTERM)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/job_output")
def api_job_output():
    path = _job.get("output")
    if path and os.path.exists(path):
        return jsonify({"path": path})
    return jsonify({"path": None})


@app.route("/api/video")
def api_video():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="video/mp4")


@app.route("/api/status")
def api_status():
    return jsonify({"running": _job["running"], "type": _job.get("type")})


@app.route("/api/stats")
def api_stats():
    try:
        summary = db.get_summary(days=7)
        # Convert Decimal to float for JSON serialisation
        for k, v in summary.items():
            if v is not None and hasattr(v, '__float__'):
                summary[k] = float(v)

        with db.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT filename, recorded_at, vehicle_count, is_night
                FROM recordings
                ORDER BY recorded_at DESC
                LIMIT 20
            """)
            recent = cursor.fetchall()
            for r in recent:
                if r["recorded_at"]:
                    r["recorded_at"] = str(r["recorded_at"])
                r["is_night"] = bool(r["is_night"])

            cursor.execute("SELECT COUNT(*) AS cnt FROM recordings")
            rec_count = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM vehicles")
            veh_count = cursor.fetchone()["cnt"]

        return jsonify({
            "summary":   summary,
            "recent":    recent,
            "db_counts": {"recordings": rec_count, "vehicles": veh_count},
        })
    except Exception as e:
        return jsonify({"error": str(e)})




@app.route("/api/frame")
def api_frame():
    """Extract a single frame from a video as JPEG."""
    path     = request.args.get("path", "")
    frame_idx = int(request.args.get("frame", 0))
    if not path or not os.path.exists(path):
        return "Not found", 404
    import cv2, io
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return "Could not read frame", 500
    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok2:
        return "Encode failed", 500
    return Response(buf.tobytes(), mimetype="image/jpeg")


@app.route("/api/zones", methods=["GET"])
def api_zones_get():
    """Read zones.json."""
    zones_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones.json")
    if not os.path.exists(zones_path):
        return jsonify({"zones": []})
    try:
        with open(zones_path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e), "zones": []})


@app.route("/api/zones", methods=["POST"])
def api_zones_post():
    """Write zones.json."""
    zones_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones.json")
    try:
        data = request.json
        with open(zones_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Traffic Analyser UI — http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)