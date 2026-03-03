"""
detect/yolo_day.py — YOLO-based vehicle detection for daytime footage.
"""

import cv2
import numpy as np

from config import (YOLO_MODEL, YOLO_CONFIDENCE, YOLO_CLASSES, YOLO_DEVICE)

# COCO class id → human label mapping (filtered to vehicles only)
CLASS_NAMES = {2: "car", 3: "motorbike", 5: "bus", 7: "truck"}

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError("Run: pip install ultralytics")
        print(f"Loading YOLO model: {YOLO_MODEL}")
        _model = YOLO(YOLO_MODEL)
    return _model


def detect(frame):
    """
    Run YOLO on a frame and return vehicle detections.

    Returns:
        centroids : list of (cx, cy)
        boxes     : list of (x, y, w, h)
        classes   : list of class label strings ("car", "truck" etc.)
        confidences: list of float confidence scores
        debug_frame: annotated frame for visualisation
    """
    model = _get_model()
    results = model(frame, conf=YOLO_CONFIDENCE, classes=YOLO_CLASSES,
                    device=YOLO_DEVICE, verbose=False)

    centroids, boxes, classes, confidences = [], [], [], []
    debug_frame = frame.copy()

    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls[0])
            if cls_id not in YOLO_CLASSES:
                continue
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            w, h = x2 - x1, y2 - y1
            cx, cy = x1 + w // 2, y1 + h // 2

            centroids.append((cx, cy))
            boxes.append((x1, y1, w, h))
            classes.append(CLASS_NAMES.get(cls_id, "vehicle"))
            confidences.append(round(conf, 2))

            # Draw on debug frame
            label = f"{CLASS_NAMES.get(cls_id,'?')} {conf:.0%}"
            cv2.rectangle(debug_frame, (x1, y1), (x2, y2), (0, 200, 0), 2)
            cv2.putText(debug_frame, label, (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)

    return centroids, boxes, classes, confidences, debug_frame
