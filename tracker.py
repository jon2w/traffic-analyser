"""
tracker.py — Centroid tracker and vehicle tracker.

Handles persistent ID assignment, velocity prediction,
time-based dropout, and speed estimation.

Changes vs previous version:
  - CentroidTracker now accepts optional boxes and uses IoU as a secondary
    match criterion. When two centroids are far apart but their boxes overlap
    significantly they are still matched — fixes multi-track ghost problem
    caused by slow/stopping vehicles whose YOLO box jitters.
  - Velocity prediction is clamped to zero for near-stationary tracks so
    overshoot doesn't push the predicted position away from a stopped vehicle.
  - _net_direction now checks whether the median x-delta is large enough to
    be trustworthy; if not it falls back to net displacement.
  - MIN_TRACK_FRAMES recommendation raised to 12 (set in config.py).
"""

import math
import numpy as np
from collections import defaultdict, deque

from config import (
    MAX_DISAPPEARED_MS, MAX_TRACKER_DISTANCE,
    MIN_TRACK_FRAMES, SPEED_WINDOW_MS,
    SPEED_TRIM_FRACTION, SPEED_EMA_ALPHA,
    MIN_TRACK_DISPLACEMENT_PX,
)

# Minimum IoU to force-match two detections regardless of centroid distance.
# 0.15 is deliberately low — any meaningful box overlap counts.
_IOU_MATCH_THRESHOLD = 0.15

# If predicted velocity is below this (px/ms) treat the vehicle as stationary
# and don't extrapolate its position.  At 15 fps a frame is ~67 ms, so
# 0.05 px/ms ≈ 3 px/frame — pure jitter territory.
_MIN_PREDICT_VELOCITY = 0.05

# Minimum absolute median x-delta (px/frame) before we trust the
# median-delta direction.  Below this the track is too slow/jittery and we
# fall back to net displacement.
_MIN_DIRECTION_DELTA = 2.0


# ─── IoU helper ───────────────────────────────────────────────────────────────

def _iou(box_a, box_b):
    """
    Intersection-over-Union for two boxes in (x, y, w, h) format.
    Returns 0.0 if either box has zero area.
    """
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b

    ix = max(ax, bx)
    iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)

    inter_w = max(0, ix2 - ix)
    inter_h = max(0, iy2 - iy)
    inter   = inter_w * inter_h

    area_a = aw * ah
    area_b = bw * bh
    union  = area_a + area_b - inter

    if union <= 0:
        return 0.0
    return inter / union


# ─── Centroid Tracker ─────────────────────────────────────────────────────────

class CentroidTracker:
    """
    Assigns persistent IDs to detections across frames.

    Matching strategy (in order of priority):
      1. If boxes are provided and two detections have IoU >= _IOU_MATCH_THRESHOLD,
         match them regardless of centroid distance.
      2. Otherwise use predicted centroid distance with MAX_TRACKER_DISTANCE gate.

    Velocity prediction is suppressed for near-stationary tracks to prevent
    overshoot from pushing a stopped vehicle's predicted position away from
    where it actually is.
    """

    def __init__(self):
        self.next_id      = 0
        self.objects      = {}   # id → (cx, cy)
        self.boxes        = {}   # id → (x, y, w, h) — may be None
        self.velocities   = {}   # id → (vx, vy) px/ms
        self.last_seen_ms = {}   # id → timestamp_ms

    def register(self, centroid, ts, box=None):
        self.objects[self.next_id]      = centroid
        self.boxes[self.next_id]        = box
        self.velocities[self.next_id]   = (0.0, 0.0)
        self.last_seen_ms[self.next_id] = ts
        self.next_id += 1

    def deregister(self, oid):
        for d in (self.objects, self.boxes, self.velocities, self.last_seen_ms):
            d.pop(oid, None)

    def update(self, centroids, ts, boxes=None):
        """
        Parameters
        ----------
        centroids : list of (cx, cy)
        ts        : current timestamp in ms
        boxes     : optional list of (x, y, w, h), same length as centroids.
                    Pass None or omit to use centroid-only matching.
        """
        # Normalise boxes list
        if boxes is None or len(boxes) != len(centroids):
            boxes = [None] * len(centroids)

        # Drop timed-out tracks
        for oid in list(self.last_seen_ms):
            if ts - self.last_seen_ms[oid] > MAX_DISAPPEARED_MS:
                self.deregister(oid)

        if not centroids:
            return dict(self.objects)

        if not self.objects:
            for c, b in zip(centroids, boxes):
                self.register(c, ts, b)
            return dict(self.objects)

        oids = list(self.objects.keys())

        # Predict where each tracked object will be this frame
        predicted = []
        for oid in oids:
            cx, cy   = self.objects[oid]
            vx, vy   = self.velocities[oid]
            dt       = ts - self.last_seen_ms[oid]
            speed    = math.hypot(vx, vy)
            if speed >= _MIN_PREDICT_VELOCITY:
                predicted.append((cx + vx * dt, cy + vy * dt))
            else:
                # Vehicle was near-stationary — don't extrapolate
                predicted.append((cx, cy))

        # ── Build matching matrices ───────────────────────────────────────────

        n_tracked = len(oids)
        n_new     = len(centroids)

        # Centroid-distance matrix (predicted positions vs new detections)
        D = np.full((n_tracked, n_new), np.inf)
        for i, (px, py) in enumerate(predicted):
            for j, (nx, ny) in enumerate(centroids):
                D[i, j] = math.hypot(px - nx, py - ny)

        # IoU matrix (last known box vs new box) — only populated when both exist
        IOU = np.zeros((n_tracked, n_new))
        for i, oid in enumerate(oids):
            if self.boxes[oid] is None:
                continue
            for j, nb in enumerate(boxes):
                if nb is None:
                    continue
                IOU[i, j] = _iou(self.boxes[oid], nb)

        # ── Greedy matching ───────────────────────────────────────────────────
        # Build a combined priority score.  IoU matches get priority (negative
        # score so they sort first); distance matches are secondary.
        scores = []
        for i in range(n_tracked):
            for j in range(n_new):
                iou_val  = IOU[i, j]
                dist_val = D[i, j]
                if iou_val >= _IOU_MATCH_THRESHOLD:
                    # Force-match: score < 0 so it sorts before any distance match
                    scores.append((-iou_val, i, j, "iou"))
                elif dist_val <= MAX_TRACKER_DISTANCE:
                    scores.append((dist_val, i, j, "dist"))

        scores.sort(key=lambda x: x[0])

        used_r, used_c = set(), set()
        for score_val, r, c, match_type in scores:
            if r in used_r or c in used_c:
                continue
            oid  = oids[r]
            pcx, pcy = self.objects[oid]
            ncx, ncy = centroids[c]
            dt   = max(ts - self.last_seen_ms[oid], 1)
            self.velocities[oid]   = ((ncx - pcx) / dt, (ncy - pcy) / dt)
            self.objects[oid]      = centroids[c]
            self.boxes[oid]        = boxes[c]
            self.last_seen_ms[oid] = ts
            used_r.add(r)
            used_c.add(c)

        # Register new detections that weren't matched to any existing track
        for c in set(range(n_new)) - used_c:
            self.register(centroids[c], ts, boxes[c])

        return dict(self.objects)


# ─── Vehicle Tracker ──────────────────────────────────────────────────────────

class VehicleTracker:
    """
    Wraps CentroidTracker with per-track analytics:
    speed estimation, direction, vehicle class, and finalised records.
    """

    def __init__(self, fps, ppm_left, ppm_right, zone_name):
        self.fps        = fps
        self.ppm_left   = ppm_left
        self.ppm_right  = ppm_right
        self.zone_name  = zone_name
        self.ct         = CentroidTracker()
        self.paths      = defaultdict(lambda: deque(maxlen=300))  # (x,y,ms)
        self.speeds     = {}
        self.directions = {}   # id → direction from detector (if available)
        self.classes    = {}   # id → vehicle class from YOLO
        self.confs      = {}   # id → confidence from YOLO
        self.counted    = set()
        self.vehicles   = []
        self.finalised  = set()

    def update(self, centroids, ts, directions=None, classes=None,
               confs=None, boxes=None):
        """
        Parameters
        ----------
        centroids  : list of (cx, cy)
        ts         : timestamp in ms
        directions : optional list of direction strings
        classes    : optional list of class label strings
        confs      : optional list of float confidences
        boxes      : optional list of (x, y, w, h) — passed to CentroidTracker
                     for IoU-assisted matching
        """
        objects    = self.ct.update(centroids, ts, boxes=boxes)
        active_ids = set(objects.keys())

        if directions or classes or confs:
            for i, cent in enumerate(centroids):
                best_oid, best_dist = None, 999999
                for oid, obj_cent in objects.items():
                    d = math.hypot(cent[0] - obj_cent[0], cent[1] - obj_cent[1])
                    if d < best_dist:
                        best_dist, best_oid = d, oid
                if best_oid is not None and best_dist < MAX_TRACKER_DISTANCE:
                    if directions and i < len(directions):
                        if directions[i] != "unknown":
                            self.directions[best_oid] = directions[i]
                    if classes and i < len(classes):
                        self.classes[best_oid] = classes[i]
                    if confs and i < len(confs):
                        self.confs[best_oid] = confs[i]

        for oid, cent in objects.items():
            self.paths[oid].append((cent[0], cent[1], ts))
            path = self.paths[oid]

            if len(path) >= 2:
                t_now = path[-1][2]
                ref = path[0]
                for p in path:
                    if t_now - p[2] <= SPEED_WINDOW_MS:
                        ref = p
                        break
                x1, y1, t1 = ref
                x2, y2, t2 = path[-1]
                dt_ms = t2 - t1
                if dt_ms >= 50:
                    px_dist = math.hypot(x2 - x1, y2 - y1)
                    xs = [p[0] for p in path]
                    going_right = xs[-1] > xs[0]
                    ppm = self.ppm_right if going_right else self.ppm_left
                    spd = (px_dist / ppm) / (dt_ms / 1000.0) * 3.6
                    if 1.0 < spd < 200 and px_dist > 5.0:
                        prev = self.speeds.get(oid, spd)
                        self.speeds[oid] = (SPEED_EMA_ALPHA * prev +
                                            (1 - SPEED_EMA_ALPHA) * spd)

            if oid not in self.counted and len(path) >= MIN_TRACK_FRAMES:
                self.counted.add(oid)

        for oid in list(self.paths):
            if oid not in active_ids and oid not in self.finalised:
                self._finalise(oid)

        return objects

    def _net_direction(self, path):
        """
        Determine direction of travel from the median x-delta across the
        first 80% of track points.

        If the median delta is too small to be trusted (slow or stationary
        vehicle, jitter-dominated track) fall back to net x-displacement.
        This prevents noise flipping the direction label on slow vehicles.
        """
        pts = list(path)
        if len(pts) < 2:
            return "right" if pts[-1][0] > pts[0][0] else "left"

        # Use first 80% of points to avoid end-of-track merge contamination
        cutoff  = max(2, int(len(pts) * 0.8))
        pts     = pts[:cutoff]
        x_deltas = [pts[i][0] - pts[i-1][0] for i in range(1, len(pts))]
        median_delta = float(np.median(x_deltas))

        if abs(median_delta) >= _MIN_DIRECTION_DELTA:
            # Median delta is large enough to be reliable
            return "right" if median_delta > 0 else "left"

        # Fall back: use total net displacement over the available points
        total_dx = pts[-1][0] - pts[0][0]
        if abs(total_dx) > 0:
            return "right" if total_dx > 0 else "left"

        # Last resort: use full path net displacement
        full_pts = list(path)
        return "right" if full_pts[-1][0] >= full_pts[0][0] else "left"

    def _finalise(self, oid):
        self.finalised.add(oid)
        path = self.paths[oid]

        if len(path) < MIN_TRACK_FRAMES:
            self.paths.pop(oid, None)
            return

        # Reject stationary detections
        total_displacement = math.hypot(path[-1][0] - path[0][0],
                                        path[-1][1] - path[0][1])
        if total_displacement < MIN_TRACK_DISPLACEMENT_PX:
            self.paths.pop(oid, None)
            return

        xs  = [p[0] for p in path]
        dur = (path[-1][2] - path[0][2]) / 1000.0

        # Direction: use median x-velocity — robust to merges and occlusion.
        # If a detector-provided direction exists but contradicts the overall
        # movement, trust the movement.
        net_dir   = self._net_direction(path)
        det_dir   = self.directions.get(oid)
        direction = (net_dir
                     if (not det_dir or det_dir == "unknown" or det_dir != net_dir)
                     else det_dir)

        # Final speed from middle portion of track (avoids edge distortion)
        n    = len(path)
        trim = max(1, int(n * SPEED_TRIM_FRACTION))
        mid  = list(path)[trim: n - trim]
        speed = self.speeds.get(oid, 0.0)
        if len(mid) >= 2:
            x1, y1, t1 = mid[0]
            x2, y2, t2 = mid[-1]
            dt_ms = t2 - t1
            if dt_ms > 50:
                px_dist = math.hypot(x2 - x1, y2 - y1)
                ppm = self.ppm_right if net_dir == "right" else self.ppm_left
                s = (px_dist / ppm) / (dt_ms / 1000.0) * 3.6
                if 1.0 < s < 200:
                    speed = s

        vehicle_class = self.classes.get(oid, "unknown")
        confidence    = self.confs.get(oid)

        record = {
            "id":            oid,
            "zone":          self.zone_name,
            "direction":     direction,
            "speed_kmh":     round(speed, 1),
            "vehicle_class": vehicle_class,
            "confidence":    confidence,
            "track_frames":  len(path),
            "duration_s":    round(dur, 1),
            "first_seen_ms": int(path[0][2]),
            "last_seen_ms":  int(path[-1][2]),
            "track_points":  [(int(p[2]), p[0], p[1]) for p in path],
        }
        self.vehicles.append(record)

        dir_arrow = "->" if direction == "right" else "<-"
        print(f"  [{self.zone_name}] Vehicle #{len(self.vehicles):03d} | "
              f"{dir_arrow} {direction} | ~{speed:.0f} km/h | "
              f"{vehicle_class} | {len(path)} frames ({dur:.1f}s)")

        self.paths.pop(oid, None)

    def active_label(self, oid):
        """Return live speed+direction string for overlay."""
        path = self.paths.get(oid)
        if not path or len(path) < 2:
            return ""
        d   = "->" if self._net_direction(path) == "right" else "<-"
        spd = self.speeds.get(oid, 0.0)
        cls = self.classes.get(oid, "")
        return f"{d} {spd:.0f}km/h {cls}".strip()

    def finalise_all(self):
        """Finalise any remaining active tracks at end of video."""
        for oid in list(self.paths):
            if oid not in self.finalised:
                self._finalise(oid)
