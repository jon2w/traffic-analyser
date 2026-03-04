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
  - Colour histogram appearance matching added to CentroidTracker.
    Each track stores an HSV histogram of its bounding box, updated as an EMA
    each frame so gradual lighting changes don't break it.  During matching,
    candidates whose histogram correlation is below HIST_MIN_CORR are penalised
    so visually dissimilar detections are not merged even if their boxes overlap
    or their centroids are close.  Brief occlusion is handled gracefully because
    the stored histogram persists with the track ID until the vehicle reappears.
"""

import math
import numpy as np
from collections import defaultdict, deque

import cv2

from config import (
    MAX_DISAPPEARED_MS, MAX_TRACKER_DISTANCE,
    MIN_TRACK_FRAMES, SPEED_WINDOW_MS,
    SPEED_TRIM_FRACTION, SPEED_EMA_ALPHA,
    MIN_TRACK_DISPLACEMENT_PX,
    HIST_MIN_CORR, HIST_EMA_ALPHA,
)

# Minimum IoU to force-match two detections regardless of centroid distance.
_IOU_MATCH_THRESHOLD = 0.15

# If predicted velocity is below this (px/ms) treat the vehicle as stationary
# and don't extrapolate its position.  At 15 fps a frame is ~67 ms, so
# 0.05 px/ms ≈ 3 px/frame — pure jitter territory.
_MIN_PREDICT_VELOCITY = 0.05

# Minimum absolute median x-delta (px/frame) before we trust the
# median-delta direction.  Below this fall back to net displacement.
_MIN_DIRECTION_DELTA = 2.0

# Histogram parameters — H and S channels of HSV (ignore V for lighting robustness)
_HIST_BINS  = [16, 16]
_HIST_RANGE = [0, 180, 0, 256]   # H: 0-180, S: 0-255


# ─── Histogram helpers ────────────────────────────────────────────────────────

def _compute_hist(frame, box):
    """
    Compute a normalised 2D HS histogram for the crop defined by box.

    Uses H and S channels of HSV only — ignoring V means the descriptor is
    robust to brightness differences between shaded and sunlit parts of the road.

    Returns a flattened float32 array, or None if the crop is degenerate.
    """
    x, y, w, h = box
    fh, fw = frame.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(fw, x + w), min(fh, y + h)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, _HIST_BINS, _HIST_RANGE)
    cv2.normalize(hist, hist, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)
    return hist.flatten().astype(np.float32)


def _hist_corr(hist_a, hist_b):
    """
    Pearson correlation between two histograms in [−1, 1].
    1.0 = identical appearance.  Returns 0.0 if either histogram is None.
    """
    if hist_a is None or hist_b is None:
        return 0.0
    return float(cv2.compareHist(
        hist_a.reshape(-1, 1),
        hist_b.reshape(-1, 1),
        cv2.HISTCMP_CORREL,
    ))


# ─── IoU helper ───────────────────────────────────────────────────────────────

def _iou(box_a, box_b):
    """IoU for two (x, y, w, h) boxes."""
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    ix  = max(ax, bx);  iy  = max(ay, by)
    ix2 = min(ax+aw, bx+bw); iy2 = min(ay+ah, by+bh)
    inter = max(0, ix2-ix) * max(0, iy2-iy)
    union = aw*ah + bw*bh - inter
    return inter / union if union > 0 else 0.0


# ─── Centroid Tracker ─────────────────────────────────────────────────────────

class CentroidTracker:
    """
    Assigns persistent IDs to detections across frames.

    Matching priority (lowest score wins, greedy):
      1. IoU ≥ threshold AND histogram correlation ≥ HIST_MIN_CORR
         → score = -iou  (highest priority, negative)
      2. IoU ≥ threshold BUT histogram correlation < HIST_MIN_CORR
         → downgraded to penalised distance match (visually different vehicles
           whose boxes briefly overlap are not merged)
      3. Distance ≤ MAX_TRACKER_DISTANCE AND histogram OK
         → score = distance
      4. Distance ≤ MAX_TRACKER_DISTANCE BUT histogram poor
         → score = distance + MAX_TRACKER_DISTANCE  (penalised, last resort)

    When no stored histogram exists yet (first few frames of a new track)
    the histogram check is skipped and matching falls through to IoU/distance
    as before, so new vehicles are not starved of an ID.

    Brief occlusion: the stored histogram persists with the track ID through
    MAX_DISAPPEARED_MS of absence, so a vehicle that briefly disappears behind
    a sign or another vehicle is re-matched by appearance when it reappears.
    """

    def __init__(self):
        self.next_id      = 0
        self.objects      = {}   # id → (cx, cy)
        self.boxes        = {}   # id → (x, y, w, h) or None
        self.velocities   = {}   # id → (vx, vy) px/ms
        self.last_seen_ms = {}   # id → timestamp ms
        self.histograms   = {}   # id → float32 array or None

    def register(self, centroid, ts, box=None, hist=None):
        self.objects[self.next_id]      = centroid
        self.boxes[self.next_id]        = box
        self.velocities[self.next_id]   = (0.0, 0.0)
        self.last_seen_ms[self.next_id] = ts
        self.histograms[self.next_id]   = hist
        self.next_id += 1

    def deregister(self, oid):
        for d in (self.objects, self.boxes, self.velocities,
                  self.last_seen_ms, self.histograms):
            d.pop(oid, None)

    def update(self, centroids, ts, boxes=None, frame=None):
        """
        Parameters
        ----------
        centroids : list of (cx, cy)
        ts        : timestamp ms
        boxes     : list of (x, y, w, h), same length as centroids, or None
        frame     : full BGR frame for histogram computation, or None
                    (histogram matching is skipped gracefully when None)
        """
        if boxes is None or len(boxes) != len(centroids):
            boxes = [None] * len(centroids)

        # Compute histograms for incoming detections
        new_hists = [
            (_compute_hist(frame, b) if (frame is not None and b is not None) else None)
            for b in boxes
        ]

        # Expire timed-out tracks
        for oid in list(self.last_seen_ms):
            if ts - self.last_seen_ms[oid] > MAX_DISAPPEARED_MS:
                self.deregister(oid)

        if not centroids:
            return dict(self.objects)

        if not self.objects:
            for c, b, h in zip(centroids, boxes, new_hists):
                self.register(c, ts, b, h)
            return dict(self.objects)

        oids = list(self.objects.keys())

        # ── Position prediction ───────────────────────────────────────────────
        predicted = []
        for oid in oids:
            cx, cy = self.objects[oid]
            vx, vy = self.velocities[oid]
            dt     = ts - self.last_seen_ms[oid]
            if math.hypot(vx, vy) >= _MIN_PREDICT_VELOCITY:
                predicted.append((cx + vx*dt, cy + vy*dt))
            else:
                predicted.append((cx, cy))

        n_t, n_n = len(oids), len(centroids)

        # ── Distance matrix ───────────────────────────────────────────────────
        D = np.full((n_t, n_n), np.inf)
        for i, (px, py) in enumerate(predicted):
            for j, (nx, ny) in enumerate(centroids):
                D[i, j] = math.hypot(px-nx, py-ny)

        # ── IoU matrix ────────────────────────────────────────────────────────
        IOU = np.zeros((n_t, n_n))
        for i, oid in enumerate(oids):
            if self.boxes[oid] is None:
                continue
            for j, nb in enumerate(boxes):
                if nb is not None:
                    IOU[i, j] = _iou(self.boxes[oid], nb)

        # ── Histogram correlation matrix ──────────────────────────────────────
        # If no stored histogram yet, treat correlation as 1.0 (don't penalise)
        CORR = np.ones((n_t, n_n))
        for i, oid in enumerate(oids):
            sh = self.histograms[oid]
            if sh is None:
                continue   # stays 1.0 — no penalty for trackless newcomers
            for j, nh in enumerate(new_hists):
                CORR[i, j] = _hist_corr(sh, nh)

        # ── Score every candidate pair ────────────────────────────────────────
        scores = []
        for i in range(n_t):
            for j in range(n_n):
                iou_val  = IOU[i, j]
                dist_val = D[i, j]
                hist_ok  = CORR[i, j] >= HIST_MIN_CORR

                if iou_val >= _IOU_MATCH_THRESHOLD:
                    if hist_ok:
                        scores.append((-iou_val, i, j))          # best
                    elif dist_val <= MAX_TRACKER_DISTANCE:
                        # boxes overlap but looks different — penalise heavily
                        scores.append((dist_val + MAX_TRACKER_DISTANCE, i, j))
                elif dist_val <= MAX_TRACKER_DISTANCE:
                    if hist_ok:
                        scores.append((dist_val, i, j))           # normal
                    else:
                        scores.append((dist_val + MAX_TRACKER_DISTANCE, i, j))

        scores.sort(key=lambda x: x[0])

        # ── Greedy assignment ─────────────────────────────────────────────────
        used_r, used_c = set(), set()
        for _, r, c in scores:
            if r in used_r or c in used_c:
                continue
            oid = oids[r]
            pcx, pcy = self.objects[oid]
            ncx, ncy = centroids[c]
            dt = max(ts - self.last_seen_ms[oid], 1)

            self.velocities[oid]   = ((ncx-pcx)/dt, (ncy-pcy)/dt)
            self.objects[oid]      = centroids[c]
            self.boxes[oid]        = boxes[c]
            self.last_seen_ms[oid] = ts

            # EMA histogram update
            nh = new_hists[c]
            oh = self.histograms[oid]
            if nh is not None:
                self.histograms[oid] = (
                    nh.copy() if oh is None
                    else HIST_EMA_ALPHA * oh + (1.0 - HIST_EMA_ALPHA) * nh
                )

            used_r.add(r); used_c.add(c)

        # Register unmatched detections as new tracks
        for c in set(range(n_n)) - used_c:
            self.register(centroids[c], ts, boxes[c], new_hists[c])

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
        self.paths      = defaultdict(lambda: deque(maxlen=300))
        self.speeds     = {}
        self.directions = {}
        self.classes    = {}
        self.confs      = {}
        self.counted    = set()
        self.vehicles   = []
        self.finalised  = set()

    def update(self, centroids, ts, directions=None, classes=None,
               confs=None, boxes=None, frame=None):
        """
        Parameters
        ----------
        centroids  : list of (cx, cy)
        ts         : timestamp ms
        directions : optional list of direction strings
        classes    : optional list of class label strings
        confs      : optional list of float confidences
        boxes      : optional list of (x, y, w, h)
        frame      : optional full BGR frame for histogram matching
        """
        objects    = self.ct.update(centroids, ts, boxes=boxes, frame=frame)
        active_ids = set(objects.keys())

        if directions or classes or confs:
            for i, cent in enumerate(centroids):
                best_oid, best_dist = None, 999999
                for oid, obj_cent in objects.items():
                    d = math.hypot(cent[0]-obj_cent[0], cent[1]-obj_cent[1])
                    if d < best_dist:
                        best_dist, best_oid = d, oid
                if best_oid is not None and best_dist < MAX_TRACKER_DISTANCE:
                    if directions and i < len(directions) and directions[i] != "unknown":
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
                    px_dist = math.hypot(x2-x1, y2-y1)
                    going_right = path[-1][0] > path[0][0]
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
        Direction from median x-delta of first 80% of track.
        Falls back to net displacement if delta is too small to trust.
        """
        pts = list(path)
        if len(pts) < 2:
            return "right" if pts[-1][0] > pts[0][0] else "left"

        cutoff   = max(2, int(len(pts) * 0.8))
        pts      = pts[:cutoff]
        x_deltas = [pts[i][0] - pts[i-1][0] for i in range(1, len(pts))]
        med      = float(np.median(x_deltas))

        if abs(med) >= _MIN_DIRECTION_DELTA:
            return "right" if med > 0 else "left"

        dx = pts[-1][0] - pts[0][0]
        if dx != 0:
            return "right" if dx > 0 else "left"

        full = list(path)
        return "right" if full[-1][0] >= full[0][0] else "left"

    def _finalise(self, oid):
        self.finalised.add(oid)
        path = self.paths[oid]

        if len(path) < MIN_TRACK_FRAMES:
            self.paths.pop(oid, None)
            return

        disp = math.hypot(path[-1][0]-path[0][0], path[-1][1]-path[0][1])
        if disp < MIN_TRACK_DISPLACEMENT_PX:
            self.paths.pop(oid, None)
            return

        dur     = (path[-1][2] - path[0][2]) / 1000.0
        net_dir = self._net_direction(path)
        det_dir = self.directions.get(oid)
        direction = (net_dir
                     if (not det_dir or det_dir == "unknown" or det_dir != net_dir)
                     else det_dir)

        n    = len(path)
        trim = max(1, int(n * SPEED_TRIM_FRACTION))
        mid  = list(path)[trim: n - trim]
        speed = self.speeds.get(oid, 0.0)
        if len(mid) >= 2:
            x1, y1, t1 = mid[0]
            x2, y2, t2 = mid[-1]
            dt_ms = t2 - t1
            if dt_ms > 50:
                px_dist = math.hypot(x2-x1, y2-y1)
                ppm = self.ppm_right if net_dir == "right" else self.ppm_left
                s = (px_dist / ppm) / (dt_ms / 1000.0) * 3.6
                if 1.0 < s < 200:
                    speed = s

        record = {
            "id":            oid,
            "zone":          self.zone_name,
            "direction":     direction,
            "speed_kmh":     round(speed, 1),
            "vehicle_class": self.classes.get(oid, "unknown"),
            "confidence":    self.confs.get(oid),
            "track_frames":  len(path),
            "duration_s":    round(dur, 1),
            "first_seen_ms": int(path[0][2]),
            "last_seen_ms":  int(path[-1][2]),
            "track_points":  [(int(p[2]), p[0], p[1]) for p in path],
        }
        self.vehicles.append(record)

        arrow = "->" if direction == "right" else "<-"
        print(f"  [{self.zone_name}] Vehicle #{len(self.vehicles):03d} | "
              f"{arrow} {direction} | ~{speed:.0f} km/h | "
              f"{self.classes.get(oid,'unknown')} | {len(path)} frames ({dur:.1f}s)")

        self.paths.pop(oid, None)

    def active_label(self, oid):
        path = self.paths.get(oid)
        if not path or len(path) < 2:
            return ""
        d   = "->" if self._net_direction(path) == "right" else "<-"
        spd = self.speeds.get(oid, 0.0)
        cls = self.classes.get(oid, "")
        return f"{d} {spd:.0f}km/h {cls}".strip()

    def finalise_all(self):
        for oid in list(self.paths):
            if oid not in self.finalised:
                self._finalise(oid)
