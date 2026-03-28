# Traffic Analyser

Analyses vehicle traffic from MotionEye recordings. Detects, tracks, and
measures speed of vehicles across configurable road zones using YOLOv8 (day)
or colour-based light detection (night).

## Architecture

```
IP Camera
    │
    │  RTSP/HTTP stream
    ▼
Raspberry Pi  ──────────────────────────────────────►  NAS (MotionEye)
                                                          records to:
                                                          /volume1/traffic/recordings/
                                                          CameraX/YYYY-MM-DD/HH-MM-SS.mp4
                                                               │
                               ┌───────────────────────────────┘
                               │  claim file, download, process
                               ▼
                        Worker machine(s)
                        (any PC on the LAN)
                             batch.py
                               │
                               │  vehicle count, speed, class
                               ▼
                           MariaDB
```

The NAS is only used for storage — it is too slow to run analysis at high
traffic volumes. One or more worker machines on the local network pick up
unprocessed recordings, analyse them, and write results to MariaDB. Multiple
workers can run simultaneously; the database layer handles claiming so the
same file is never processed twice.

## Files

```
traffic-analyser/
├── batch.py            Primary runner — discovers, claims, and processes files
├── monitor.py          Alternative continuous watcher (single-machine use)
├── analyse.py          Process a single video file (called by batch/monitor)
├── tracker.py          Centroid tracker + vehicle tracker
├── database.py         MariaDB interface, schema setup, and file claiming
├── config.py           All tunable parameters
├── zones_loader.py     Loads zone polygon definitions from zones.json
├── zones.json          Zone polygon definitions (edit via tune_zones.py)
├── tune_zones.py       Interactive zone polygon editor
├── watchdog.sh         NAS cron script — keeps web_ui.py running
├── run_batch.sh        Simple batch launcher (activates venv, runs batch.py)
├── sync_from_pi.sh     (Legacy) NAS pulls recordings from Pi via rsync
├── sync_to_nas.sh      (Legacy) Pi pushes recordings to NAS via rsync
├── requirements.txt
└── detect/
    ├── __init__.py     YOLOv8 detection (day mode)
    └── night.py        Colour light detection (night mode)
```

## Setup

### 1. Install dependencies (on each worker machine)

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config.py`:
- `DB_HOST`, `DB_USER`, `DB_PASSWORD` — MariaDB connection
- `RECORDINGS_ROOT` — path to the NAS recordings folder (network mount or UNC path)
- Calibrate `PPM_MAIN_LEFT` / `PPM_MAIN_RIGHT` for your camera distance

### 3. Set up the database (once)

```bash
python database.py --setup --root-password yourmariadbpassword
```

### 4. Run a worker

```bash
# Process all unprocessed files and exit
python batch.py

# Dry run — show what would be processed
python batch.py --dry-run

# Limit to one camera or date range
python batch.py --camera Camera1
python batch.py --since 2026-03-01

# Run overnight in the background
nohup python batch.py 2>/dev/null &
tail -f logs/batch_*.log

# Reprocess a list of previously failed files
python batch.py --from-file failed.txt
```

Multiple workers can run simultaneously on different machines — each claims
files atomically via the database before processing them.

### 5. Test a single file

```bash
python analyse.py --input /path/to/recording.mp4 --no-show
python analyse.py --input /path/to/recording.mp4 --output ~/annotated.mov
```

### 6. NAS watchdog (optional)

`watchdog.sh` is intended to be run as a cron job on the NAS to keep the
web UI process alive. It does **not** run analysis — that runs on worker machines.

```
# On NAS — crontab -e
*/5 * * * * /volume1/traffic/watchdog.sh
```

## Calibration

### Zone polygons

Zones are defined as polygons in `zones.json` using `(x_fraction, y_fraction)`
coordinates. Edit them interactively with:

```bash
python tune_zones.py --video /path/to/recording.mp4
# or
python tune_zones.py --frame /path/to/frame.jpg
```

Controls: `1–9` select zone, click to add/remove points, `S` save, `Q` quit.

### Pixels per metre (PPM)

1. Record a clip with a parked car of known length (~4.5 m for a typical car)
2. Measure the car's pixel width at the distance relevant to each lane
3. `PPM = pixel_width / car_length_metres`
4. Set `PPM_MAIN_LEFT` and `PPM_MAIN_RIGHT` in `config.py`
   (they differ because the two lanes are at different distances from the camera)

### Night ROI

`NIGHT_ROI_TOP` and `NIGHT_ROI_BOTTOM` define the horizontal band where
lights are detected. Set them to exclude background buildings and foreground
bushes. The orange lines in annotated output show the current boundaries.

## Database queries

```sql
-- Vehicles in last 7 days
SELECT direction, AVG(speed_kmh), COUNT(*)
FROM vehicles v
JOIN recordings r ON v.recording_id = r.id
WHERE r.recorded_at > DATE_SUB(NOW(), INTERVAL 7 DAY)
GROUP BY direction;

-- Busiest hours
SELECT HOUR(r.recorded_at) AS hour, COUNT(v.id) AS count
FROM vehicles v
JOIN recordings r ON v.recording_id = r.id
GROUP BY hour ORDER BY hour;

-- Vehicles by class
SELECT vehicle_class, COUNT(*), AVG(speed_kmh)
FROM vehicles
GROUP BY vehicle_class;
```
