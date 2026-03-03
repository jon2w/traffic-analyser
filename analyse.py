"""
analyse.py — Process a single traffic recording.

Usage:
    python analyse.py --input video.mp4
    python analyse.py --input video.mp4 --output ~/Downloads/annotated.mov
    python analyse.py --input video.mp4 --no-show --save-db
    python analyse.py --input video.mp4 --day      # force day mode
    python analyse.py --input video.mp4 --night    # force night mode
"""

import argparse
import os
import sys
import time
import math
import re
from datetime import datetime

import cv2
import numpy as np

from config import (
    NIGHT_BRIGHTNESS_THRESHOLD,
    THUMBNAILS_ROOT,
    PPM_MAIN_LEFT,
    PPM_MAIN_RIGHT,
)
from zones_loader import ZONES
from tracker import VehicleTracker


# ─── Args ─────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(description="Traffic analyser")
    p.add_argument("--input",    required=True, help="Video file path")
    p.add_argument("--output",   default=None,  help="Save annotated video here")
    p.add_argument("--night",    action="store_true", help="Force night mode")
    p.add_argument("--day",      action="store_true", help="Force day mode")
    p.add_argument("--show",     action="store_true", default=True)
    p.add_argument("--no-show",  dest="show", action="store_false")
    p.add_argument("--save-db",  action="store_true",
                   help="Save results to MariaDB")
    p.add_argument("--save-thumbs", action="store_true",
                   help="Save vehicle thumbnail images")
    return p.parse_args()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_night(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() < NIGHT_BRIGHTNESS_THRESHOLD


def point_in_polygon(x, y, polygon, frame_w, frame_h):
    """Check if (x,y) pixel is inside a zone polygon (defined as fractions)."""
    pts = np.array([(int(px * frame_w), int(py * frame_h))
                    for px, py in polygon], dtype=np.int32)
    return cv2.pointPolygonTest(pts, (float(x), float(y)), False) >= 0


def open_writer(output_path, fps, frame_w, frame_h):
    for codec in ["avc1", "mp4v", "MJPG"]:
        fourcc = cv2.VideoWriter_fourcc(*codec)
        w = cv2.VideoWriter(output_path, fourcc, fps, (frame_w, frame_h))
        if w.isOpened():
            print(f"Video writer: {codec} → {output_path}")
            return w
    print("WARNING: Could not open video writer")
    return None


def parse_recording_time(filename):
    """Try to parse datetime from MotionEye filename like 2026-02-28/08-20-37.mp4"""
    basename = os.path.basename(filename)
    # Try HH-MM-SS.mp4
    m = re.search(r'(\d{2})-(\d{2})-(\d{2})\.mp4', basename)
    parent = os.path.basename(os.path.dirname(filename))
    # Try YYYY-MM-DD parent folder
    dm = re.match(r'(\d{4})-(\d{2})-(\d{2})', parent)
    if m and dm:
        try:
            return datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)),
                            int(m.group(1)),  int(m.group(2)),  int(m.group(3)))
        except ValueError:
            pass
    return datetime.now()


def save_thumbnail(frame, vehicle_id, recording_id, output_dir):
    """Save a cropped thumbnail of the vehicle's last known position."""
    os.makedirs(output_dir, exist_ok=True)
    filename = f"rec{recording_id:06d}_v{vehicle_id:04d}.jpg"
    path = os.path.join(output_dir, filename)
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


# ─── Overlay ──────────────────────────────────────────────────────────────────

def draw_overlay(frame, zone_trackers, night_mode, fps_actual, frame_w, frame_h):
    from config import NIGHT_ROI_TOP, NIGHT_ROI_BOTTOM, ZONES

    # Zone polygons
    for zone in ZONES:
        pts = np.array([(int(px * frame_w), int(py * frame_h))
                        for px, py in zone["polygon"]], dtype=np.int32)
        cv2.polylines(frame, [pts], True, (0, 255, 255), 1)

    # Night ROI lines
    if night_mode:
        rt = int(frame_h * NIGHT_ROI_TOP)
        rb = int(frame_h * NIGHT_ROI_BOTTOM)
        cv2.line(frame, (0, rt), (frame_w, rt), (255, 130, 0), 1)
        cv2.line(frame, (0, rb), (frame_w, rb), (255, 130, 0), 1)

    # Tracks and labels for each zone tracker
    total_count = 0
    total_active = 0
    for vt in zone_trackers:
        objects = vt.ct.objects
        total_active += len(objects)
        total_count  += len(vt.counted)
        for oid, cent in objects.items():
            path = vt.paths.get(oid)
            if path and len(path) > 1:
                pts = np.array([(int(p[0]), int(p[1])) for p in path],
                               dtype=np.int32)
                cv2.polylines(frame, [pts], False, (255, 100, 0), 2)
            label = vt.active_label(oid)
            if label:
                cv2.putText(frame, label,
                            (int(cent[0]) - 40, int(cent[1]) - 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

    # HUD
    mode_str = "NIGHT" if night_mode else "DAY"
    cv2.rectangle(frame, (0, 0), (340, 90), (0, 0, 0), -1)
    cv2.putText(frame, f"Vehicles counted: {total_count}",
                (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
    cv2.putText(frame, f"Active tracks:    {total_active}",
                (8, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 255), 2)
    cv2.putText(frame, f"Mode: {mode_str}  FPS: {fps_actual:.1f}",
                (8, 74), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)

    return frame


# ─── Main ─────────────────────────────────────────────────────────────────────

def analyse(input_path, output_path=None, force_night=False, force_day=False,
            show=True, save_db=False, save_thumbs=False):

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {input_path}")

    fps     = cap.get(cv2.CAP_PROP_FPS) or 10.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_s = total_frames / fps if fps > 0 else 0

    print(f"Video: {frame_w}×{frame_h} @ {fps:.1f} fps  "
          f"({total_frames} frames, {duration_s:.1f}s)")
    print(f"File:  {input_path}")

    writer = open_writer(output_path, fps, frame_w, frame_h) if output_path else None

    # Determine night/day from first frame
    ret, first_frame = cap.read()
    if not ret:
        raise RuntimeError("Could not read first frame")
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    if force_night:
        night_mode = True
    elif force_day:
        night_mode = False
    else:
        night_mode = is_night(first_frame)

    print(f"Mode:  {'NIGHT' if night_mode else 'DAY'}")

    # Load detectors
    if night_mode:
        from detect.night import detect as night_detect
        detector = night_detect
    else:
        from detect.yolo_day import detect as day_detect
        detector = day_detect

    # One tracker per zone
    zone_trackers = []
    for zone in ZONES:
        ppm_l = zone.get("ppm_left",  PPM_MAIN_LEFT)
        ppm_r = zone.get("ppm_right", PPM_MAIN_RIGHT)
        zone_trackers.append(VehicleTracker(fps, ppm_l, ppm_r, zone["name"]))

    recorded_at = parse_recording_time(input_path)
    t_prev      = time.time()
    fps_actual  = fps

    print("\nProcessing — press Q to quit\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        ts = cap.get(cv2.CAP_PROP_POS_MSEC)

        # Detect
        if night_mode:
            centroids, boxes, directions, vtypes, debug_frame, debug_mask = \
                detector(frame)
        else:
            centroids, boxes, classes, confidences, debug_frame = \
                detector(frame)
            directions = ["unknown"] * len(centroids)
            vtypes     = ["yolo"]   * len(centroids)

        # Route detections to zone trackers based on centroid location
        for vt, zone in zip(zone_trackers, ZONES):
            zone_cents = []
            zone_dirs  = []
            zone_cls   = [] if not night_mode else None
            zone_conf  = [] if not night_mode else None

            for i, (cx, cy) in enumerate(centroids):
                if point_in_polygon(cx, cy, zone["polygon"], frame_w, frame_h):
                    zone_cents.append((cx, cy))
                    zone_dirs.append(directions[i])
                    if not night_mode:
                        zone_cls.append(classes[i])
                        zone_conf.append(confidences[i])

            vt.update(zone_cents, ts,
                      directions=zone_dirs,
                      classes=zone_cls,
                      confs=zone_conf)

        # FPS counter
        now        = time.time()
        fps_actual = 0.9 * fps_actual + 0.1 * (1.0 / max(now - t_prev, 1e-6))
        t_prev     = now

        annotated = draw_overlay(frame.copy(), zone_trackers,
                                 night_mode, fps_actual, frame_w, frame_h)
        if writer:
            writer.write(annotated)
        if show:
            cv2.imshow("Traffic Analyser", annotated)
            cv2.imshow("Debug", debug_frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    # Finalise all remaining tracks
    for vt in zone_trackers:
        vt.finalise_all()

    cap.release()
    if writer:
        writer.release()
    try:
        cv2.destroyAllWindows()
    except cv2.error:
    	pass

    # Collect all vehicles across zones
    all_vehicles = []
    for vt in zone_trackers:
        all_vehicles.extend(vt.vehicles)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"Total vehicles : {len(all_vehicles)}")
    if all_vehicles:
        speeds = [v["speed_kmh"] for v in all_vehicles if v["speed_kmh"] > 1]
        right  = sum(1 for v in all_vehicles if v["direction"] == "right")
        left   = sum(1 for v in all_vehicles if v["direction"] == "left")
        if speeds:
            print(f"Average speed  : {sum(speeds)/len(speeds):.1f} km/h")
            print(f"Max speed      : {max(speeds):.1f} km/h")
        print(f"Going right →  : {right}")
        print(f"Going left  ←  : {left}")
        by_class = {}
        for v in all_vehicles:
            by_class[v["vehicle_class"]] = by_class.get(v["vehicle_class"], 0) + 1
        for cls, count in sorted(by_class.items()):
            print(f"  {cls:12s}: {count}")
    print(f"{'─'*55}\n")

    # ── Database ──────────────────────────────────────────────────────────────
    if save_db and all_vehicles:
        try:
            import database as db
            recording_id = db.insert_recording(
                filename    = os.path.abspath(input_path),
                camera_name = "Camera1",
                recorded_at = recorded_at,
                duration_s  = duration_s,
                frame_width = frame_w, frame_height = frame_h,
                fps         = fps,
                is_night    = night_mode,
            )
            for v in all_vehicles:
                thumb_path = None
                if save_thumbs:
                    # We'd need to save a frame crop here — placeholder for now
                    pass
                vehicle_id = db.insert_vehicle(
                    recording_id = recording_id,
                    zone         = v["zone"],
                    direction    = v["direction"],
                    speed_kmh    = v["speed_kmh"],
                    vehicle_class = v["vehicle_class"],
                    confidence   = v.get("confidence"),
                    track_frames = v["track_frames"],
                    duration_s   = v["duration_s"],
                    first_seen_ms = v["first_seen_ms"],
                    last_seen_ms  = v["last_seen_ms"],
                    thumbnail_path = thumb_path,
                    detected_at  = recorded_at,
                )
                db.insert_track_points(vehicle_id, v["track_points"])

            db.update_recording_count(recording_id, len(all_vehicles))
            print(f"Saved to database: recording_id={recording_id}, "
                  f"{len(all_vehicles)} vehicles")
        except Exception as e:
            print(f"Database error: {e}")

    return all_vehicles


if __name__ == "__main__":
    args = get_args()
    analyse(
        input_path   = args.input,
        output_path  = args.output,
        force_night  = args.night,
        force_day    = args.day,
        show         = args.show,
        save_db      = args.save_db,
        save_thumbs  = args.save_thumbs,
    )
