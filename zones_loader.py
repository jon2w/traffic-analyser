"""
zones_loader.py — Load zone definitions from zones.json.

This is the single source of truth for zone config.
analyse.py, monitor.py and any other scripts should import from here.
"""

import json
import os

ZONES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones.json")

# Fallback ppm values if not specified in a zone
DEFAULT_PPM_LEFT  = 44.0
DEFAULT_PPM_RIGHT = 33.0


def load_zones():
    """Load and return the list of zone dicts from zones.json."""
    if not os.path.exists(ZONES_PATH):
        raise FileNotFoundError(
            f"zones.json not found at {ZONES_PATH}\n"
            "Run tune_zones.py to create it."
        )
    with open(ZONES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    zones = data.get("zones", [])
    if not zones:
        raise ValueError("zones.json contains no zones.")

    # Normalise: ensure polygon points are tuples, fill defaults
    for z in zones:
        z["polygon"]   = [tuple(pt) for pt in z["polygon"]]
        z.setdefault("ppm_left",  DEFAULT_PPM_LEFT)
        z.setdefault("ppm_right", DEFAULT_PPM_RIGHT)

    return zones


# Convenience — load once at import time so callers can just do:
#   from zones_loader import ZONES
ZONES = load_zones()
