#!/usr/bin/env python3
"""
tune_zones.py — Interactive polygon editor for traffic analyser zones.

Reads and writes zones.json in the same directory as this script.

Usage:
    python tune_zones.py --video <path_to_recording.mp4>
    python tune_zones.py --frame <path_to_image.jpg>

Controls:
    1-9         — select zone by number
    ] / [       — next / previous zone
    N           — create a new zone (prompts in terminal)
    D           — delete the active zone entirely
    C           — clear all points from active zone
    Left-click  — add point (or delete if clicking near an existing point)
    S           — save all zones to zones.json and quit
    Q / Esc     — quit without saving
    Space       — advance one frame (video mode)
    F           — skip forward ~5 seconds (video mode)
"""

import cv2
import sys
import os
import json
import argparse
import numpy as np

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
ZONES_PATH     = os.path.join(SCRIPT_DIR, "zones.json")
SNAP_RADIUS_PX = 12   # px — click within this distance of a point to delete it

PALETTE = [
    (0,   0,   255),   # red
    (0,   255, 255),   # yellow
    (0,   255, 0  ),   # green
    (255, 0,   0  ),   # blue
    (255, 0,   255),   # magenta
    (0,   165, 255),   # orange
]

ZONE_TYPES = ["side_on", "end_on"]


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_zones():
    if not os.path.exists(ZONES_PATH):
        print(f"No zones.json found at {ZONES_PATH} — starting fresh.")
        return []
    with open(ZONES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    zones = data.get("zones", [])
    for z in zones:
        z["polygon"] = [list(pt) for pt in z["polygon"]]
    print(f"Loaded {len(zones)} zone(s) from {ZONES_PATH}")
    return zones


def save_zones(zones):
    for z in zones:
        if len(z["polygon"]) < 3:
            print(f"ERROR: zone '{z['name']}' has fewer than 3 points — aborting save.")
            return False
    data = {"zones": zones}
    with open(ZONES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nSaved {len(zones)} zone(s) to {ZONES_PATH}")
    for z in zones:
        print(f"  {z['name']} ({z['type']}): {len(z['polygon'])} points")
    return True


# ── Point snapping ────────────────────────────────────────────────────────────

def nearest_point(px, py, polygon, W, H):
    """Return (index, pixel_distance) of nearest polygon point to pixel (px, py)."""
    best_idx  = None
    best_dist = float("inf")
    for i, (fx, fy) in enumerate(polygon):
        dx = px - int(fx * W)
        dy = py - int(fy * H)
        d  = (dx*dx + dy*dy) ** 0.5
        if d < best_dist:
            best_dist = d
            best_idx  = i
    return best_idx, best_dist


# ── Rendering ─────────────────────────────────────────────────────────────────

def colour_for(index, active):
    c = PALETTE[index % len(PALETTE)]
    if not active:
        return tuple(int(v * 0.5) for v in c)
    return c


def render(frame, zones, active_idx, W, H, hover_pt_idx):
    overlay = frame.copy()

    for i, zone in enumerate(zones):
        is_active = (i == active_idx)
        pts = zone["polygon"]
        col = colour_for(i, is_active)

        # Filled + outlined polygon
        if len(pts) >= 3:
            px = np.array([(int(x*W), int(y*H)) for x, y in pts], dtype=np.int32)
            cv2.fillPoly(overlay, [px], col)
            cv2.polylines(overlay, [px], isClosed=True,
                          color=col, thickness=3 if is_active else 1)

        # Individual points
        for j, (fx, fy) in enumerate(pts):
            cx, cy    = int(fx*W), int(fy*H)
            is_hover  = is_active and (j == hover_pt_idx)
            if is_hover:
                # Red filled + white ring = "will delete"
                cv2.circle(overlay, (cx, cy), 10, (0, 0, 200), -1)
                cv2.circle(overlay, (cx, cy), 10, (255, 255, 255), 2)
            else:
                cv2.circle(overlay, (cx, cy), 7 if is_active else 4, col, -1)
            cv2.putText(overlay, str(j), (cx+10, cy-8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1)

        # Extra ring on last added point of active zone
        if is_active and pts:
            last_j = len(pts) - 1
            if last_j != hover_pt_idx:
                lx, ly = int(pts[-1][0]*W), int(pts[-1][1]*H)
                cv2.circle(overlay, (lx, ly), 10, col, 2)

    out = cv2.addWeighted(overlay, 0.4, frame, 0.6, 0)

    # HUD (top)
    hud = [
        "ZONE EDITOR  |  1-9 or [ / ] = select zone  |  N=new  D=delete zone",
        "Click=add point  |  Click ON point=delete point  |  C=clear all points",
        "S=save+quit  |  Q=quit without saving  |  Space=next frame  |  F=skip 5s",
    ]
    for i, line in enumerate(hud):
        y = 22 + i * 22
        cv2.putText(out, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 3)
        cv2.putText(out, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)

    # Zone list (bottom)
    if not zones:
        cv2.putText(out, "No zones — press N to create one", (10, H - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2)
    else:
        for i, zone in enumerate(reversed(zones)):
            idx       = len(zones) - 1 - i
            is_active = (idx == active_idx)
            col       = colour_for(idx, True)
            marker    = ">" if is_active else " "
            label     = (f"{marker} [{idx+1}] {zone['name']} "
                         f"({zone['type']}): {len(zone['polygon'])} pts")
            y = H - 15 - i * 24
            cv2.putText(out, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
            cv2.putText(out, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)

    return out


# ── New zone prompt (terminal) ────────────────────────────────────────────────

def prompt_new_zone(existing_names):
    print("\n── New Zone ──────────────────────────────")
    while True:
        name = input("  Zone name (e.g. 'side_street'): ").strip()
        if not name:
            print("  Name cannot be empty.")
            continue
        if name in existing_names:
            print(f"  '{name}' already exists.")
            continue
        break

    print(f"  Zone types: {', '.join(ZONE_TYPES)}")
    while True:
        ztype = input(f"  Zone type [{ZONE_TYPES[0]}]: ").strip() or ZONE_TYPES[0]
        if ztype in ZONE_TYPES:
            break
        print(f"  Must be one of: {', '.join(ZONE_TYPES)}")

    zone = {"name": name, "type": ztype, "polygon": []}

    if ztype == "side_on":
        try:
            ppm_l = float(input("  ppm_left  (pixels/metre, left-bound)  [44.0]: ").strip() or 44.0)
            ppm_r = float(input("  ppm_right (pixels/metre, right-bound) [33.0]: ").strip() or 33.0)
            zone["ppm_left"]  = ppm_l
            zone["ppm_right"] = ppm_r
        except ValueError:
            print("  Invalid value — using defaults (44.0 / 33.0)")
            zone["ppm_left"]  = 44.0
            zone["ppm_right"] = 33.0

    print(f"  Zone '{name}' created — now click points on the frame.")
    print("──────────────────────────────────────────\n")
    return zone


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tune zone polygons interactively.")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", help="Path to a recording (.mp4)")
    group.add_argument("--frame", help="Path to a still image")
    args = parser.parse_args()

    # ── Load source frame ─────────────────────────────────────────────────────
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
        fps_vid = cap.get(cv2.CAP_PROP_FPS) or 25
        # Start 10 s in for a representative frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps_vid * 10))
        ret, base_frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, base_frame = cap.read()
        if not ret:
            print("Error: could not read frame from video")
            sys.exit(1)

    H, W  = base_frame.shape[:2]
    zones = load_zones()

    # Shared mutable state (readable/writable from mouse callback)
    state = {
        "active_idx": 0 if zones else None,
        "hover_idx":  None,   # index of nearest point in active zone, if within snap radius
    }

    print(f"Frame: {W}x{H}")
    print("Click to add points. Click an existing point to delete it.")
    print("Press N to create a zone, S to save.\n")

    # ── Mouse callback ────────────────────────────────────────────────────────
    def mouse_cb(event, x, y, flags, param):
        idx = state["active_idx"]
        if idx is None or not zones:
            return

        polygon = zones[idx]["polygon"]

        # Always update hover highlight on any mouse event
        if polygon:
            ni, nd = nearest_point(x, y, polygon, W, H)
            state["hover_idx"] = ni if nd <= SNAP_RADIUS_PX else None
        else:
            state["hover_idx"] = None

        if event == cv2.EVENT_LBUTTONDOWN:
            if state["hover_idx"] is not None:
                removed = polygon.pop(state["hover_idx"])
                print(f"Deleted point {state['hover_idx']} "
                      f"({removed[0]:.4f}, {removed[1]:.4f}) "
                      f"from '{zones[idx]['name']}'")
                state["hover_idx"] = None
            else:
                fx, fy = round(x / W, 4), round(y / H, 4)
                polygon.append([fx, fy])

    cv2.namedWindow("Zone Editor", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Zone Editor", min(W, 1400), min(H, 800))
    cv2.setMouseCallback("Zone Editor", mouse_cb)

    # ── Event loop ────────────────────────────────────────────────────────────
    while True:
        idx     = state["active_idx"]
        display = render(base_frame, zones,
                         idx if idx is not None else -1,
                         W, H, state["hover_idx"])
        cv2.imshow("Zone Editor", display)
        key = cv2.waitKey(30) & 0xFF

        # Select zone by number key
        if ord('1') <= key <= ord('9'):
            n = key - ord('1')
            if n < len(zones):
                state["active_idx"] = n
                state["hover_idx"]  = None
                print(f"Active zone: {zones[n]['name']}")

        # Next zone
        elif key == ord(']'):
            if zones:
                state["active_idx"] = (state["active_idx"] + 1) % len(zones)
                state["hover_idx"]  = None
                print(f"Active zone: {zones[state['active_idx']]['name']}")

        # Previous zone
        elif key == ord('['):
            if zones:
                state["active_idx"] = (state["active_idx"] - 1) % len(zones)
                state["hover_idx"]  = None
                print(f"Active zone: {zones[state['active_idx']]['name']}")

        # New zone
        elif key == ord('n'):
            new_zone = prompt_new_zone([z["name"] for z in zones])
            zones.append(new_zone)
            state["active_idx"] = len(zones) - 1
            state["hover_idx"]  = None

        # Delete active zone
        elif key == ord('d'):
            if zones and state["active_idx"] is not None:
                removed = zones.pop(state["active_idx"])
                print(f"Deleted zone: {removed['name']}")
                if zones:
                    state["active_idx"] = min(state["active_idx"], len(zones) - 1)
                else:
                    state["active_idx"] = None
                state["hover_idx"] = None

        # Clear points from active zone
        elif key == ord('c'):
            if zones and state["active_idx"] is not None:
                zones[state["active_idx"]]["polygon"] = []
                state["hover_idx"] = None
                print(f"Cleared points for: {zones[state['active_idx']]['name']}")

        # Save + quit
        elif key == ord('s'):
            if save_zones(zones):
                break

        # Quit without saving
        elif key in (ord('q'), 27):
            print("Quit without saving.")
            break

        # Next frame (video only)
        elif key == ord(' ') and cap:
            ret, frame = cap.read()
            if ret:
                base_frame = frame
            else:
                print("End of video — looping back")
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, base_frame = cap.read()

        # Skip 5 s forward (video only)
        elif key == ord('f') and cap:
            fps_vid = cap.get(cv2.CAP_PROP_FPS) or 25
            pos     = cap.get(cv2.CAP_PROP_POS_FRAMES)
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos + fps_vid * 5)
            ret, frame = cap.read()
            if ret:
                base_frame = frame

    cv2.destroyAllWindows()
    if cap:
        cap.release()


if __name__ == "__main__":
    main()
