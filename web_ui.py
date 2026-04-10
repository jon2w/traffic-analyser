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
import auth

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


# ── Job queue API ─────────────────────────────────────────────────────────────

@app.route("/api/jobs/next", methods=["POST"])
def api_jobs_next():
    """
    Claim the next pending job for a worker.
    Populates the queue from the recordings folder on first call if empty.
    POST body: { "worker_id": "hostname_OS" }
    Returns: { "job_id": int, "path": str, "rel_path": str }
          or { "empty": true } if nothing available.
    """
    data      = request.json or {}
    worker_id = data.get("worker_id", "unknown")

    # Lazily populate queue if empty
    try:
        status = db.job_queue_status()
        pending = status.get("pending", 0)
        processing = status.get("processing", 0)
        if pending == 0 and processing == 0:
            added = db.job_queue_populate(RECORDINGS_ROOT)
            if added:
                print(f"Job queue populated: {added} new jobs")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    job_id, filename = db.job_claim_next(worker_id)
    if job_id is None:
        return jsonify({"empty": True})

    rel = os.path.relpath(filename, RECORDINGS_ROOT)
    return jsonify({
        "job_id":   job_id,
        "path":     filename,
        "rel_path": rel,
    })


@app.route("/api/jobs/complete", methods=["POST"])
def api_jobs_complete():
    """
    Accept processed results from a worker and insert into the database.
    POST body: {
        "job_id":    int,
        "worker_id": str,
        "vehicles":  [ vehicle_dict, ... ]   (from analyse() return value)
    }
    The vehicle dicts must include all fields that analyse() normally produces,
    plus recording metadata is reconstructed from the filename.
    """
    data      = request.json or {}
    job_id    = data.get("job_id")
    worker_id = data.get("worker_id", "unknown")
    vehicles  = data.get("vehicles", [])

    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400

    # Look up the filename from the job
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filename FROM job_locks WHERE id=%s", (job_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "job_id not found"}), 404
            filename = row[0]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Parse recording metadata from filename
    try:
        import re
        from datetime import datetime as dt
        basename = os.path.basename(filename)
        parent   = os.path.basename(os.path.dirname(filename))
        m  = re.search(r'(\d{2})-(\d{2})-(\d{2})\.mp4', basename)
        dm = re.match(r'(\d{4})-(\d{2})-(\d{2})', parent)
        if m and dm:
            recorded_at = dt(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)),
                             int(m.group(1)),  int(m.group(2)),  int(m.group(3)))
        else:
            recorded_at = dt.now()

        # Get video metadata (duration, fps etc) from file if accessible
        frame_w, frame_h, fps, duration_s, is_night = 1280, 720, 15.0, 0.0, False
        if os.path.exists(filename):
            import cv2
            cap = cv2.VideoCapture(filename)
            fps      = cap.get(cv2.CAP_PROP_FPS) or 15.0
            frame_w  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            frame_h  = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            frames   = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration_s = frames / fps if fps > 0 else 0
            # Guess night from hour
            is_night = recorded_at.hour < 6 or recorded_at.hour >= 21
            cap.release()

    except Exception as e:
        return jsonify({"ok": False, "error": f"metadata error: {e}"}), 500

    # Insert into database
    try:
        recording_id = db.insert_recording(
            filename    = os.path.abspath(filename),
            camera_name = "Camera1",
            recorded_at = recorded_at,
            duration_s  = duration_s,
            frame_width = frame_w, frame_height = frame_h,
            fps         = fps,
            is_night    = bool(is_night),
        )
        for v in vehicles:
            vehicle_id = db.insert_vehicle(
                recording_id  = recording_id,
                zone          = v.get("zone", "main"),
                direction     = v.get("direction", "unknown"),
                speed_kmh     = v.get("speed_kmh", 0.0),
                vehicle_class = v.get("vehicle_class", "unknown"),
                confidence    = v.get("confidence"),
                track_frames  = v.get("track_frames", 0),
                duration_s    = v.get("duration_s", 0.0),
                first_seen_ms = v.get("first_seen_ms", 0),
                last_seen_ms  = v.get("last_seen_ms", 0),
                thumbnail_path = None,
                detected_at   = recorded_at,
            )
            if v.get("track_points"):
                db.insert_track_points(vehicle_id, v["track_points"])

        db.update_recording_count(recording_id, len(vehicles))
        db.job_complete(job_id, worker_id)

        return jsonify({"ok": True, "recording_id": recording_id,
                        "vehicles": len(vehicles)})

    except Exception as e:
        db.job_fail(job_id, worker_id, str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/jobs/fail", methods=["POST"])
def api_jobs_fail():
    """
    Release or fail a job.
    POST body: { "job_id": int, "worker_id": str, "reason": str }
    If reason is "dry_run" or "download_failed", job is released back to pending.
    Otherwise it is marked failed.
    """
    data      = request.json or {}
    job_id    = data.get("job_id")
    worker_id = data.get("worker_id", "unknown")
    reason    = data.get("reason", "")

    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400

    try:
        if reason in ("dry_run", "download_failed"):
            db.job_release(job_id, worker_id)
        else:
            retryable = data.get("retryable", False)
            db.job_fail(job_id, worker_id, reason, retryable=retryable)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/jobs/status")
def api_jobs_status():
    """Return job queue status counts."""
    try:
        return jsonify(db.job_queue_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Remote submission API (authenticated) ──────────────────────────────────────

@app.route("/api/submit_results", methods=["POST"])
@auth.require_auth
def api_submit_results(user):
    """
    Accept analysis results from a remote user who has run the analysis locally.
    This is the same flow as worker.py, but for users submitting their own files
    without downloading from the server.
    
    Request JSON:
    {
        "filename": "intersection_2024-04-10_14-30-00.mp4",
        "location_name": "Downtown Intersection",
        "camera_name": "User Camera",
        "recorded_at": "2024-04-10T14:30:00",
        "duration_s": 600.0,
        "frame_width": 1280,
        "frame_height": 720,
        "fps": 25.0,
        "is_night": false,
        "vehicles": [
            {
                "zone": "main_road",
                "direction": "left",
                "speed_kmh": 45.2,
                "vehicle_class": "car",
                "confidence": 0.95,
                "track_frames": 120,
                "duration_s": 4.8,
                "first_seen_ms": 1000,
                "last_seen_ms": 5800,
                "thumbnail_path": null,
                "track_points": [
                    [0, 100, 200],      # [timestamp_ms, x, y]
                    [40, 102, 205]
                ]
            }
        ]
    }
    
    Returns:
        {
            "ok": true,
            "recording_id": 42,
            "vehicle_count": 47,
            "message": "Results submitted successfully"
        }
    """
    # Validate user can submit
    if not user["is_admin"] and user["submission_type"] != "remote":
        return jsonify({"error": "User account not configured for remote submissions"}), 403
    
    data = request.json or {}
    
    # Validate required fields
    required_fields = ["filename", "duration_s", "frame_width", "frame_height", "fps", "vehicles"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400
    
    try:
        filename = data["filename"]
        location_name = data.get("location_name", "Unknown")[:128]
        camera_name = data.get("camera_name", f"user_{user['username']}")[:64]
        recorded_at_str = data.get("recorded_at")
        duration_s = float(data["duration_s"])
        frame_width = int(data["frame_width"])
        frame_height = int(data["frame_height"])
        fps = float(data["fps"])
        is_night = bool(data.get("is_night", False))
        vehicles_data = data.get("vehicles", [])
        
        # Parse recorded_at
        if recorded_at_str:
            try:
                recorded_at = datetime.fromisoformat(recorded_at_str)
            except:
                recorded_at = datetime.now()
        else:
            recorded_at = datetime.now()
        
        # Create unique filename for server storage
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        user_dir = os.path.join(RECORDINGS_ROOT, f"remote_user_{user['id']}")
        os.makedirs(user_dir, exist_ok=True)
        
        # Store as metadata file (.json) so we can retrieve submission details
        basename = os.path.splitext(os.path.basename(filename))[0]
        safe_name = re.sub(r'[^\w\-]', '_', basename)[:64]
        stored_filename = f"{safe_name}_{ts}"
        metadata_path = os.path.join(user_dir, f"{stored_filename}.json")
        
        # Save metadata
        with open(metadata_path, 'w') as f:
            json.dump({
                "original_filename": filename,
                "user_id": user["id"],
                "username": user["username"],
                "location_name": location_name,
                "submitted_at": datetime.now().isoformat(),
                "recorded_at": recorded_at.isoformat(),
            }, f)
        
        # Insert recording into database
        recording_id = db.insert_recording(
            filename=metadata_path,  # Store path to metadata
            camera_name=camera_name,
            recorded_at=recorded_at,
            duration_s=duration_s,
            frame_width=frame_width,
            frame_height=frame_height,
            fps=fps,
            is_night=is_night,
            user_id=user["id"],
            location_name=location_name,
            submission_source="remote"
        )
        
        # Insert vehicles and track points
        vehicle_count = 0
        for v in vehicles_data:
            try:
                vehicle_id = db.insert_vehicle(
                    recording_id=recording_id,
                    zone=v.get("zone", "unknown"),
                    direction=v.get("direction", "unknown"),
                    speed_kmh=float(v.get("speed_kmh", 0)) if v.get("speed_kmh") else None,
                    vehicle_class=v.get("vehicle_class", "unknown"),
                    confidence=float(v.get("confidence", 0)) if v.get("confidence") else None,
                    track_frames=int(v.get("track_frames", 0)),
                    duration_s=float(v.get("duration_s", 0)) if v.get("duration_s") else None,
                    first_seen_ms=int(v.get("first_seen_ms", 0)),
                    last_seen_ms=int(v.get("last_seen_ms", 0)),
                    thumbnail_path=v.get("thumbnail_path"),
                    detected_at=datetime.now()
                )
                
                # Insert track points if provided
                track_points = v.get("track_points", [])
                if track_points:
                    db.insert_track_points(vehicle_id, track_points)
                
                vehicle_count += 1
            except Exception as e:
                print(f"Warning: Failed to insert vehicle: {e}")
                continue
        
        # Update vehicle count in recording
        db.update_recording_count(recording_id, vehicle_count)
        
        return jsonify({
            "ok": True,
            "recording_id": recording_id,
            "vehicle_count": vehicle_count,
            "message": f"Results submitted successfully — {vehicle_count} vehicle(s) recorded"
        }), 201
    
    except ValueError as e:
        return jsonify({"error": f"Invalid data format: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to submit results: {str(e)}"}), 500


@app.route("/api/user/jobs", methods=["GET"])
@auth.require_auth
def api_user_jobs(user):
    """
    Get list of jobs submitted by the current user.
    
    Query params:
        - status: 'pending', 'processing', 'done', 'failed' (optional)
        - limit: max results (default 50)
        - offset: pagination (default 0)
    """
    status_filter = request.args.get("status", "")
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            
            # Allow admins to see all, others only their own
            if user["is_admin"]:
                query = """
                    SELECT r.id, r.filename, r.camera_name, r.location_name,
                           r.recorded_at, r.submitted_at, r.processed_at, r.duration_s,
                           r.vehicle_count, u.username,
                           jl.status as job_status, jl.fail_reason
                    FROM recordings r
                    LEFT JOIN users u ON r.user_id = u.id
                    LEFT JOIN job_locks jl ON r.filename = jl.filename
                    WHERE 1=1
                """
                params = []
            else:
                query = """
                    SELECT r.id, r.filename, r.camera_name, r.location_name,
                           r.recorded_at, r.submitted_at, r.processed_at, r.duration_s,
                           r.vehicle_count,
                           jl.status as job_status, jl.fail_reason
                    FROM recordings r
                    LEFT JOIN job_locks jl ON r.filename = jl.filename
                    WHERE r.user_id = %s
                """
                params = [user["id"]]
            
            if status_filter:
                query += " AND jl.status = %s"
                params.append(status_filter)
            
            query += " ORDER BY r.submitted_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            
            cursor.execute(query, params)
            jobs = cursor.fetchall()
            
            # Convert Decimal objects to float
            for job in jobs:
                if job.get("duration_s") and hasattr(job["duration_s"], '__float__'):
                    job["duration_s"] = float(job["duration_s"])
            
            return jsonify({"jobs": jobs})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/results/<int:recording_id>", methods=["GET"])
@auth.require_auth
def api_user_results(user, recording_id):
    """
    Get vehicle detection results for a specific recording.
    User can only access their own recordings (unless admin).
    """
    # Check access
    if not auth.user_can_access_recording(user, recording_id):
        return jsonify({"error": "Access denied"}), 403
    
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            
            # Get recording info
            cursor.execute("""
                SELECT r.id, r.filename, r.location_name, r.recorded_at,
                       r.processed_at, r.duration_s, r.vehicle_count,
                       u.username
                FROM recordings r
                LEFT JOIN users u ON r.user_id = u.id
                WHERE r.id = %s
            """, (recording_id,))
            recording = cursor.fetchone()
            
            if not recording:
                return jsonify({"error": "Recording not found"}), 404
            
            # Get vehicles
            cursor.execute("""
                SELECT id, zone, direction, speed_kmh, vehicle_class,
                       confidence, track_frames, duration_s,
                       first_seen_ms, last_seen_ms, thumbnail_path, detected_at
                FROM vehicles
                WHERE recording_id = %s
                ORDER BY first_seen_ms
            """, (recording_id,))
            vehicles = cursor.fetchall()
            
            # Convert Decimals to float
            for v in vehicles:
                for key in ("speed_kmh", "confidence", "duration_s"):
                    if key in v and v[key] and hasattr(v[key], '__float__'):
                        v[key] = float(v[key])
            
            return jsonify({
                "recording": recording,
                "vehicles": vehicles,
                "vehicle_count": len(vehicles)
            })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin API for managing users ───────────────────────────────────────────────

@app.route("/api/admin/users", methods=["GET"])
@auth.require_admin
def api_admin_users(user):
    """
    List all users (admin only).
    """
    try:
        users = db.list_users()
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users", methods=["POST"])
@auth.require_admin
def api_admin_create_user(user):
    """
    Create a new user (admin only).
    
    Request body:
        {
            "username": "user1",
            "display_name": "User One",
            "is_admin": false,
            "submission_type": "remote"
        }
    """
    data = request.json or {}
    username = data.get("username", "").strip()
    display_name = data.get("display_name", username)
    is_admin = data.get("is_admin", False)
    submission_type = data.get("submission_type", "remote")
    
    if not username:
        return jsonify({"error": "username required"}), 400
    
    user_id, api_key = db.create_user(
        username=username,
        display_name=display_name,
        is_admin=is_admin,
        submission_type=submission_type
    )
    
    if not user_id:
        return jsonify({"error": "User already exists or database error"}), 400
    
    return jsonify({
        "ok": True,
        "user_id": user_id,
        "username": username,
        "api_key": api_key,
        "message": "User created. Save the API key — it cannot be retrieved later!"
    }), 201


@app.route("/api/admin/users/<int:user_id>/regenerate-key", methods=["POST"])
@auth.require_admin
def api_admin_regenerate_key(user, user_id):
    """Regenerate API key for a user (admin only)."""
    try:
        new_key = db.regenerate_api_key(user_id)
        return jsonify({
            "ok": True,
            "new_api_key": new_key,
            "message": "New API key generated. Save it — it cannot be retrieved later!"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users/<int:target_user_id>/deactivate", methods=["POST"])
@auth.require_admin
def api_admin_deactivate_user(user, target_user_id):
    """Deactivate a user (admin only)."""
    if target_user_id == user["id"]:
        return jsonify({"error": "Cannot deactivate yourself"}), 400
    
    try:
        db.deactivate_user(target_user_id)
        return jsonify({"ok": True, "message": "User deactivated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run migrations
    try:
        db.migrate_job_locks()
        db.migrate_multi_user_support()
    except Exception as e:
        print(f"Warning: Migration failed: {e}")
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Traffic Analyser UI — http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
