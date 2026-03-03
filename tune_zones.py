#!/usr/bin/env python3
"""
tune_zones.py — Interactive polygon editor for traffic analyser zones.

Usage:
    python tune_zones.py --video <path_to_recording.mp4>
    python tune_zones.py --frame <path_to_image.jpg>

Controls:
    1 / 2       — select zone to edit (1=main_road, 2=opposite_road)
    Left-click  — add point to current zone polygon
    Right-click — remove last point from current zone polygon
    C           — clear current zone polygon
    S           — save all zones to config.py and quit
    Q / Esc     — quit without saving
    Space       — grab next frame from video (if using --video)
    F           — step forward ~5 seconds in video
"""

import cv2
import sys
import os
import re
import argparse
import numpy as np

# ── Locate config.py ──────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.py")

# ── Zone definitions ──────────────────────────────────────────────────────────
ZONE_KEYS = ["ZONE_MAIN", "ZONE_OPPOSITE"]
ZONE_COLOURS = {
    "ZONE_MAIN":     (0, 0, 255),    # red
    "ZONE_OPPOSITE": (0, 255, 255),  # yellow
}
ZONE_LABELS = {
    "ZONE_MAIN":     "1 - main_road",
    "ZONE_OPPOSITE": "2 - opposite_road",
}

# ── Load current polygons from config.py ─────────────────────────────────────
def load_polygons():
    polygons = {}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            src = f.read()
        for key in ZONE_KEYS:
            m = re.search(
                rf'{key}\s*=\s*{{[^}}]*"polygon"\s*:\s*(\[[^\]]*\])',
                src, re.DOTALL
            )
            if m:
                pts = eval(m.group(1))
                polygons[key] = list(pts)
            else:
                polygons[key] = []
    except Exception as e:
        print(f"Warning: could not load polygons from config.py: {e}")
        for key in ZONE_KEYS:
            polygons[key] = []
    return polygons

# ── Save polygons back to config.py ──────────────────────────────────────────
def save_polygons(polygons):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        src = f.read()

    for key in ZONE_KEYS:
        pts = polygons[key]
        pts_str = "[" + ", ".join(f"({x:.4f}, {y:.4f})" for x, y in pts) + "]"
        src = re.sub(
            rf'({key}\s*=\s*{{[^}}]*"polygon"\s*:\s*)(\[[^\]]*\])',
            lambda m, s=pts_str: m.group(1) + s,
            src, flags=re.DOTALL
        )

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"Saved polygons to {CONFIG_PATH}")
    for key, pts in polygons.items():
        print(f"  {key}: {pts}")

# ── Draw everything onto the frame ───────────────────────────────────────────
def render(frame, polygons, active_zone, W, H):
    overlay = frame.copy()

    for key in ZONE_KEYS:
        pts = polygons[key]
        colour = ZONE_COLOURS[key]
        is_active = (key == active_zone)

        if len(pts) >= 3:
            px = np.array([(int(x*W), int(y*H)) for x, y in pts], dtype=np.int32)
            cv2.fillPoly(overlay, [px], colour)
            cv2.polylines(overlay, [px], isClosed=True,
                          color=colour, thickness=2 if is_active else 1)

        for i, (fx, fy) in enumerate(pts):
            cx, cy = int(fx*W), int(fy*H)
            cv2.circle(overlay, (cx, cy), 6 if is_active else 4, colour, -1)
            cv2.putText(overlay, str(i), (cx+8, cy-8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1)

        if is_active and len(pts) >= 1:
            last = (int(pts[-1][0]*W), int(pts[-1][1]*H))
            cv2.circle(overlay, last, 8, colour, 2)

    out = cv2.addWeighted(overlay, 0.4, frame, 0.6, 0)

    hud_lines = [
        "ZONE POLYGON EDITOR",
        f"Active: {ZONE_LABELS[active_zone]}",
        "1/2=switch zone  LClick=add  RClick=undo  C=clear",
        "S=save+quit  Q=quit  Space=next frame  F=skip 5s",
    ]
    for i, line in enumerate(hud_lines):
        y = 22 + i * 22
        cv2.putText(out, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(out, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

    for i, key in enumerate(ZONE_KEYS):
        pts = polygons[key]
        colour = ZONE_COLOURS[key]
        status = f"{ZONE_LABELS[key]}: {len(pts)} pts"
        y = H - 15 - i * 24
        cv2.putText(out, status, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(out, status, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 1)

    return out

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Tune zone polygons interactively.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", help="Path to a recording (.mp4)")
    group.add_argument("--frame", help="Path to a still image")
    args = parser.parse_args()

    cap = None
    if args.frame:
        base_frame = cv2.imread(args.frame)
        if base_frame is None:
            print(f"Error: could not read image {args.frame}")
            sys.exit(1)
    else:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            print(f"Error: could not open video {args.video}")
            sys.exit(1)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * 10))
        ret, base_frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, base_frame = cap.read()
        if not ret:
            print("Error: could not read frame from video")
            sys.exit(1)

    H, W = base_frame.shape[:2]
    polygons = load_polygons()
    active_zone = ZONE_KEYS[0]

    print(f"Frame size: {W}x{H}")
    print("Loaded existing polygons from config.py")
    print("Click to add points. Press S to save when done.")

    cv2.namedWindow("Zone Editor", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Zone Editor", min(W, 1400), min(H, 800))

    def mouse_cb(event, x, y, flags, param):
        fx, fy = x / W, y / H
        if event == cv2.EVENT_LBUTTONDOWN:
            polygons[active_zone].append((round(fx, 4), round(fy, 4)))
        elif event == cv2.EVENT_RBUTTONDOWN:
            if polygons[active_zone]:
                polygons[active_zone].pop()

    cv2.setMouseCallback("Zone Editor", mouse_cb)

    while True:
        display = render(base_frame, polygons, active_zone, W, H)
        cv2.imshow("Zone Editor", display)
        key = cv2.waitKey(30) & 0xFF

        if key == ord('1'):
            active_zone = ZONE_KEYS[0]
            print(f"Active zone: {active_zone}")
        elif key == ord('2'):
            active_zone = ZONE_KEYS[1]
            print(f"Active zone: {active_zone}")
        elif key == ord('c'):
            polygons[active_zone] = []
            print(f"Cleared {active_zone}")
        elif key == ord('s'):
            if all(len(polygons[k]) >= 3 for k in ZONE_KEYS):
                save_polygons(polygons)
                break
            else:
                print("Each zone needs at least 3 points before saving:")
                for k in ZONE_KEYS:
                    if len(polygons[k]) < 3:
                        print(f"  {k} only has {len(polygons[k])} point(s)")
        elif key in (ord('q'), 27):
            print("Quit without saving.")
            break
        elif key == ord(' ') and cap:
            ret, frame = cap.read()
            if ret:
                base_frame = frame
            else:
                print("End of video, looping back")
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, base_frame = cap.read()
        elif key == ord('f') and cap:
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos + fps * 5)
            ret, frame = cap.read()
            if ret:
                base_frame = frame

    cv2.destroyAllWindows()
    if cap:
        cap.release()

if __name__ == "__main__":
    main()
