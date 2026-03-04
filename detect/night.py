"""
detect/night.py — Colour-based light detection for night footage.

Detects headlights (white/yellow) and taillights (red) separately,
then pairs them to form precise single-vehicle detections.

A vehicle detection is one of:
  - Paired: one headlight blob + one taillight blob → centroid is midpoint,
            direction known immediately (white=front, red=rear)
  - Unpaired headlight: car facing camera head-on or only front visible
  - Unpaired taillight: car facing away or only rear visible
"""

import cv2
import numpy as np
import math

from config import (
    HEADLIGHT_BRIGHTNESS, HEADLIGHT_SATURATION,
    TAILLIGHT_RED_MIN, TAILLIGHT_RED_RATIO,
    LIGHT_PAIR_MAX_DIST, LIGHT_PAIR_MAX_VERT,
    LIGHT_MIN_AREA, LIGHT_MAX_AREA,
    NIGHT_ROI_TOP, NIGHT_ROI_BOTTOM,
)


def _apply_roi(mask, frame_h):
    """Blank out everything outside the road ROI."""
    roi_mask = np.zeros_like(mask)
    top    = int(frame_h * NIGHT_ROI_TOP)
    bottom = int(frame_h * NIGHT_ROI_BOTTOM)
    roi_mask[top:bottom, :] = 255
    return cv2.bitwise_and(mask, roi_mask)


def _find_blobs(binary_mask):
    """Extract blob centroids and bounding boxes from a binary mask."""
    contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    blobs = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if LIGHT_MIN_AREA < area < LIGHT_MAX_AREA:
            x, y, w, h = cv2.boundingRect(cnt)
            blobs.append({
                "cx": x + w // 2,
                "cy": y + h // 2,
                "x": x, "y": y, "w": w, "h": h,
                "area": area,
            })
    return blobs


def detect_headlights(frame):
    """Find white/yellow bright blobs (headlights)."""
    hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Bright pixels
    _, bright = cv2.threshold(gray, HEADLIGHT_BRIGHTNESS, 255, cv2.THRESH_BINARY)
    # Low saturation (white/yellow, not red/green/blue)
    _, low_sat = cv2.threshold(hsv[:, :, 1], HEADLIGHT_SATURATION, 255,
                                cv2.THRESH_BINARY_INV)
    mask = cv2.bitwise_and(bright, low_sat)
    mask = _apply_roi(mask, frame.shape[0])

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    return _find_blobs(mask), mask


def detect_taillights(frame):
    """Find red blobs (taillights)."""
    b, g, r = cv2.split(frame)
    # Red channel dominant, above minimum brightness
    red_dominant = np.zeros(frame.shape[:2], dtype=np.uint8)
    red_dominant[(r.astype(np.float32) > TAILLIGHT_RED_MIN) &
                 (r.astype(np.float32) > b.astype(np.float32) * TAILLIGHT_RED_RATIO) &
                 (r.astype(np.float32) > g.astype(np.float32) * TAILLIGHT_RED_RATIO)] = 255
    mask = _apply_roi(red_dominant, frame.shape[0])

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    return _find_blobs(mask), mask


def _pair_lights(headlights, taillights):
    """
    Pair headlight and taillight blobs that are plausibly the same vehicle.
    Returns list of vehicle dicts with centroid, direction, and component blobs.
    """
    paired_h, paired_t = set(), set()
    vehicles = []

    # Sort by area descending — match largest blobs first
    heads = sorted(enumerate(headlights), key=lambda x: -x[1]["area"])
    tails = sorted(enumerate(taillights), key=lambda x: -x[1]["area"])

    for hi, h in heads:
        if hi in paired_h:
            continue
        best_ti, best_dist = None, LIGHT_PAIR_MAX_DIST

        for ti, t in tails:
            if ti in paired_t:
                continue
            dx = abs(h["cx"] - t["cx"])
            dy = abs(h["cy"] - t["cy"])
            if dy > LIGHT_PAIR_MAX_VERT:
                continue
            dist = math.hypot(dx, dy)
            if dist < best_dist:
                best_dist = dist
                best_ti   = ti

        if best_ti is not None:
            t = taillights[best_ti]
            cx = (h["cx"] + t["cx"]) // 2
            cy = (h["cy"] + t["cy"]) // 2
            # White=front, red=rear → direction from tail to head
            direction = "right" if h["cx"] > t["cx"] else "left"
            vehicles.append({
                "cx": cx, "cy": cy,
                "direction": direction,
                "type": "paired",
                "headlight": h,
                "taillight": t,
                "bbox": (
                    min(h["x"], t["x"]),
                    min(h["y"], t["y"]),
                    abs(h["cx"] - t["cx"]) + max(h["w"], t["w"]),
                    max(h["h"], t["h"]),
                ),
            })
            paired_h.add(hi)
            paired_t.add(best_ti)

    # Unpaired headlights (head-on or only front visible)
    for hi, h in enumerate(headlights):
        if hi not in paired_h:
            vehicles.append({
                "cx": h["cx"], "cy": h["cy"],
                "direction": "unknown",
                "type": "headlight_only",
                "headlight": h, "taillight": None,
                "bbox": (h["x"], h["y"], h["w"], h["h"]),
            })

    # Unpaired taillights (departing or only rear visible)
    for ti, t in enumerate(taillights):
        if ti not in paired_t:
            vehicles.append({
                "cx": t["cx"], "cy": t["cy"],
                "direction": "unknown",
                "type": "taillight_only",
                "headlight": None, "taillight": t,
                "bbox": (t["x"], t["y"], t["w"], t["h"]),
            })

    return vehicles


def detect(frame):
    """
    Full night detection pipeline.

    Returns:
        centroids    : list of (cx, cy)
        boxes        : list of (x, y, w, h)
        directions   : list of direction strings
        vehicle_types: list of type strings
        debug_frame  : annotated frame
    """
    headlights, h_mask = detect_headlights(frame)
    taillights, t_mask = detect_taillights(frame)
    vehicles = _pair_lights(headlights, taillights)

    centroids, boxes, directions, vehicle_types = [], [], [], []
    debug_frame = frame.copy()

    # Draw ROI lines
    fh, fw = frame.shape[:2]
    roi_top    = int(fh * NIGHT_ROI_TOP)
    roi_bottom = int(fh * NIGHT_ROI_BOTTOM)
    cv2.line(debug_frame, (0, roi_top),    (fw, roi_top),    (255, 130, 0), 1)
    cv2.line(debug_frame, (0, roi_bottom), (fw, roi_bottom), (255, 130, 0), 1)

    for v in vehicles:
        centroids.append((v["cx"], v["cy"]))
        boxes.append(v["bbox"])
        directions.append(v["direction"])
        vehicle_types.append(v["type"])

        # Draw debug annotations
        x, y, w, h = v["bbox"]
        colour = (0, 200, 0) if v["type"] == "paired" else (0, 150, 255)
        cv2.rectangle(debug_frame, (x, y), (x+w, y+h), colour, 1)
        cv2.putText(debug_frame, v["direction"],
                    (v["cx"]-20, v["cy"]-8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

        # Draw headlight (white) and taillight (red) blobs
        if v["headlight"]:
            hb = v["headlight"]
            cv2.circle(debug_frame, (hb["cx"], hb["cy"]), 6, (255, 255, 255), 2)
        if v["taillight"]:
            tb = v["taillight"]
            cv2.circle(debug_frame, (tb["cx"], tb["cy"]), 6, (0, 0, 255), 2)

    # Combined debug mask
    debug_mask = cv2.addWeighted(h_mask, 0.5, t_mask, 0.5, 0)

    return centroids, boxes, directions, vehicle_types, debug_frame, debug_mask
