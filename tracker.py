"""
tracker.py — Centroid tracker and vehicle tracker.

Handles persistent ID assignment, velocity prediction,
time-based dropout, and speed estimation.
"""

import math
import numpy as np
from collections import defaultdict, deque

from config import (
    MAX_DISAPPEARED_MS, MAX_TRACKER_DISTANCE,
    MIN_TRACK_FRAMES, SPEED_WINDOW_MS,
    SPEED_TRIM_FRACTION, SPEED_EMA_ALPHA,
)


class CentroidTracker:
    """
    Assigns persistent IDs to blobs across frames.
    Uses velocity prediction so fast vehicles are matched correctly
    even when they move a large distance between frames.
    Dropout is time-based so uneven framerates don't break tracks.
    """

    def __init__(self):
        self.next_id      = 0
        self.objects      = {}   # id → (cx, cy)
        self.velocities   = {}   # id → (vx, vy) px/ms
        self.last_seen_ms = {}   # id → timestamp_ms

    def register(self, centroid, ts):
        self.objects[self.next_id]      = centroid
        self.velocities[self.next_id]   = (0.0, 0.0)
        self.last_seen_ms[self.next_id] = ts
        self.next_id += 1

    def deregister(self, oid):
        for d in (self.objects, self.velocities, self.last_seen_ms):
            d.pop(oid, None)

    def update(self, centroids, ts):
        # Drop timed-out tracks
        for oid in list(self.last_seen_ms):
            if ts - self.last_seen_ms[oid] > MAX_DISAPPEARED_MS:
                self.deregister(oid)

        if not centroids:
            return dict(self.objects)

        if not self.objects:
            for c in centroids:
                self.register(c, ts)
            return dict(self.objects)

        oids = list(self.objects.keys())

        # Predict where each tracked object will be this frame
        predicted = []
        for oid in oids:
            cx, cy = self.objects[oid]
            vx, vy = self.velocities[oid]
            dt = ts - self.last_seen_ms[oid]
            predicted.append((cx + vx * dt, cy + vy * dt))

        # Build distance matrix between predictions and new detections
        D = np.zeros((len(predicted), len(centroids)))
        for i, (px, py) in enumerate(predicted):
            for j, (nx, ny) in enumerate(centroids):
                D[i, j] = math.hypot(px - nx, py - ny)

        # Greedy matching: closest pair first
        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]
        used_r, used_c = set(), set()

        for r, c in zip(rows, cols):
            if r in used_r or c in used_c:
                continue
            if D[r, c] > MAX_TRACKER_DISTANCE:
                continue
            oid = oids[r]
            pcx, pcy = self.objects[oid]
            ncx, ncy = centroids[c]
            dt = max(ts - self.last_seen_ms[oid], 1)
            self.velocities[oid]   = ((ncx - pcx) / dt, (ncy - pcy) / dt)
            self.objects[oid]      = centroids[c]
            self.last_seen_ms[oid] = ts
            used_r.add(r)
            used_c.add(c)

        # Register new blobs that weren't matched
        for c in set(range(len(centroids))) - used_c:
            self.register(centroids[c], ts)

        return dict(self.objects)


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

    def update(self, centroids, ts, directions=None, classes=None, confs=None):
        """
        Update tracker with new detections.

        centroids  : list of (cx, cy)
        ts         : timestamp in milliseconds
        directions : optional list of direction strings matching centroids
        classes    : optional list of vehicle class strings
        confs      : optional list of confidence floats
        """
        objects    = self.ct.update(centroids, ts)
        active_ids = set(objects.keys())

        # Map new centroids to object IDs for metadata assignment
        # We do this by matching centroids to objects by position
        if directions or classes or confs:
            for i, cent in enumerate(centroids):
                # Find closest object ID to this centroid
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

            # Speed from a window of positions
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

        # Finalise tracks that have been dropped by the centroid tracker
        for oid in list(self.paths):
            if oid not in active_ids and oid not in self.finalised:
                self._finalise(oid)

        return objects

    def _finalise(self, oid):
        self.finalised.add(oid)
        path = self.paths[oid]

        if len(path) < MIN_TRACK_FRAMES:
            self.paths.pop(oid, None)
            return

        xs  = [p[0] for p in path]
        dur = (path[-1][2] - path[0][2]) / 1000.0

        # Direction: prefer detector-provided direction, fall back to movement
        direction = self.directions.get(oid)
        if not direction or direction == "unknown":
            direction = "right" if xs[-1] > xs[0] else "left"

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
                ppm = self.ppm_right if xs[-1] > xs[0] else self.ppm_left
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

        dir_arrow = "→" if direction == "right" else "←"
        print(f"  [{self.zone_name}] Vehicle #{len(self.vehicles):03d} | "
              f"{dir_arrow} {direction} | ~{speed:.0f} km/h | "
              f"{vehicle_class} | {len(path)} frames ({dur:.1f}s)")

        self.paths.pop(oid, None)

    def active_label(self, oid):
        """Return live speed+direction string for overlay."""
        path = self.paths.get(oid)
        if not path or len(path) < 2:
            return ""
        xs = [p[0] for p in path]
        d  = "->" if xs[-1] > xs[0] else "<-"
        spd = self.speeds.get(oid, 0.0)
        cls = self.classes.get(oid, "")
        return f"{d} {spd:.0f}km/h {cls}".strip()

    def finalise_all(self):
        """Finalise any remaining active tracks at end of video."""
        for oid in list(self.paths):
            if oid not in self.finalised:
                self._finalise(oid)
