"""
config.py — All tunable parameters for the traffic analyser.
Edit this file to calibrate for your specific camera setup.
Zone polygons are defined in zones.json (edit with tune_zones.py).
"""

# =============================================================================
# DATABASE
# =============================================================================
DB_HOST     = "localhost"       # MariaDB host
DB_PORT     = 3306
DB_NAME     = "traffic"
DB_USER     = "traffic_user"
DB_PASSWORD = "changeme"        # change this!

# =============================================================================
# PATHS
# =============================================================================
# Root folder where MotionEye recordings are synced to on the NAS
RECORDINGS_ROOT = "/volume1/traffic/recordings"

# Where to save vehicle thumbnail crops
THUMBNAILS_ROOT = "/volume1/traffic/thumbnails"

# =============================================================================
# CAMERA & SPEED CALIBRATION
# =============================================================================
# Pixels per metre at 1280x720.
# Measure a car (~4.5m) in a frame and divide pixel width by 4.5.
# These are fallback defaults — per-zone values in zones.json take priority.
PPM_MAIN_LEFT  = 44.0   # R->L lane of main road (~12m from camera)
PPM_MAIN_RIGHT = 33.0   # L->R lane of main road (~16m from camera)

# =============================================================================
# DETECTION
# =============================================================================

# Night mode threshold - frame mean brightness below this = night
NIGHT_BRIGHTNESS_THRESHOLD = 60

# --- Day mode (YOLO) ---
YOLO_MODEL       = "yolov8n.pt"    # nano=fast, small=yolov8s.pt, medium=yolov8m.pt
YOLO_CONFIDENCE  = 0.35            # minimum detection confidence
YOLO_CLASSES     = [2, 3, 5, 7]   # COCO: car, motorcycle, bus, truck
YOLO_DEVICE      = "cpu"           # "cpu" or "cuda" if GPU available

# --- Night mode (colour light detection) ---
# Headlight detection (white/yellow blobs)
HEADLIGHT_BRIGHTNESS  = 200        # minimum brightness to be a headlight
HEADLIGHT_SATURATION  = 80         # maximum saturation (white/yellow, not coloured)

# Taillight detection (red blobs)
TAILLIGHT_RED_MIN     = 150        # minimum red channel value
TAILLIGHT_RED_RATIO   = 2.0        # red must be this many times brighter than blue

# Night ROI - restrict detection to road area (fraction of frame height)
NIGHT_ROI_TOP         = 0.55
NIGHT_ROI_BOTTOM      = 0.92

# Maximum pixel distance between headlight and taillight to be paired as one car
# At 1280px wide, a 4.5m car at 12m distance ~= 200px long
LIGHT_PAIR_MAX_DIST   = 280
LIGHT_PAIR_MAX_VERT   = 60         # max vertical offset between paired lights

# Minimum blob area for light detection
LIGHT_MIN_AREA        = 80
LIGHT_MAX_AREA        = 8000

# =============================================================================
# TRACKING
# =============================================================================
MAX_DISAPPEARED_MS   = 1000    # drop track after this many ms unseen
MAX_TRACKER_DISTANCE = 500     # max px a detection can jump between frames
MIN_TRACK_FRAMES     = 8       # minimum frames to count as real vehicle
SPEED_WINDOW_MS      = 600     # measure speed over this time window
SPEED_TRIM_FRACTION  = 0.10    # trim this fraction from each end for final speed
SPEED_EMA_ALPHA      = 0.75    # smoothing (0=none, 1=never updates)

# =============================================================================
# MONITOR
# =============================================================================
# How often to check for new recordings (seconds)
MONITOR_POLL_INTERVAL = 60

# Minimum file age before processing (seconds) - ensures recording is complete
MONITOR_MIN_FILE_AGE  = 30