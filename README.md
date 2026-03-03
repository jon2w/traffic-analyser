# Traffic Analyser

Analyses vehicle traffic from MotionEye recordings. Detects, tracks, and
measures speed of vehicles on two road zones — a side-on main road and an
end-on opposite road.

## Architecture

```
Pi camera → MotionEye records → rsync to NAS
                                     ↓
                             monitor.py watches folder
                                     ↓
                             analyse.py processes files
                                     ↓
                             MariaDB stores results
```

## Files

```
traffic_analyser/
├── config.py          All tunable parameters — edit this first
├── analyse.py         Process a single video file
├── monitor.py         Watch folder, auto-process new recordings
├── tracker.py         Centroid tracker + vehicle tracker
├── database.py        MariaDB interface + schema setup
├── sync_to_nas.sh     Pi cron script to rsync recordings to NAS
├── requirements.txt
└── detect/
    ├── yolo_day.py    YOLOv8 detection (day mode)
    └── night.py       Colour light detection (night mode)
```

## Setup

### 1. Install dependencies (on NAS or Mac)
```bash
pip install -r requirements.txt
```

### 2. Configure
Edit `config.py`:
- Set `DB_HOST`, `DB_USER`, `DB_PASSWORD`
- Set `RECORDINGS_ROOT` to where rsync puts files
- Calibrate `PPM_MAIN_LEFT` / `PPM_MAIN_RIGHT` for your camera

### 3. Set up database
```bash
python database.py --setup --root-password yourmariadbpassword
```

### 4. Set up rsync on the Pi
```bash
# Copy sync script to Pi
scp sync_to_nas.sh jon@raspberrypi:~/sync_to_nas.sh
ssh jon@raspberrypi chmod +x ~/sync_to_nas.sh

# Set up SSH key auth from Pi to NAS (so rsync doesn't need a password)
ssh-keygen -t ed25519          # on the Pi
ssh-copy-id traffic_sync@nas   # copy key to NAS

# Add to Pi crontab (syncs every 5 minutes)
crontab -e
# Add: */5 * * * * /home/jon/sync_to_nas.sh >> /home/jon/sync.log 2>&1
```

### 5. Test on a single file
```bash
python analyse.py --input /path/to/recording.mp4 --no-show
python analyse.py --input /path/to/recording.mp4 --output ~/annotated.mov
```

### 6. Run the monitor
```bash
# Process any pending files and exit
python monitor.py --once

# Run continuously
python monitor.py

# On NAS as a scheduled task (DSM Task Scheduler)
# Command: /usr/local/bin/python3 /volume1/traffic/traffic_analyser/monitor.py --once
# Schedule: every 5 minutes
```

## Calibration

### Pixels per metre (PPM)
1. Record a clip with a parked car of known length (~4.5m for typical car)
2. Open in any video viewer, measure car's pixel width
3. `PPM = pixel_width / car_length_metres`
4. Set `PPM_MAIN_LEFT` and `PPM_MAIN_RIGHT` in config.py
   (they differ because the two lanes are at different distances)

### Night ROI
The `NIGHT_ROI_TOP` and `NIGHT_ROI_BOTTOM` parameters define the horizontal
band where lights are detected at night. Set them to exclude background
buildings and foreground bushes. The orange lines in the annotated video
show the current boundaries.

### Zone polygons
Zones are defined as polygons in config.py using (x_fraction, y_fraction)
coordinates. Run `analyse.py` with `--show` to see zone boundaries drawn
on the video, and adjust until they cover the right areas.

## Database queries

```sql
-- Vehicles in last 7 days
SELECT direction, AVG(speed_kmh), COUNT(*)
FROM vehicles v
JOIN recordings r ON v.recording_id = r.id
WHERE r.recorded_at > DATE_SUB(NOW(), INTERVAL 7 DAY)
GROUP BY direction;

-- Busiest hours
SELECT HOUR(r.recorded_at) as hour, COUNT(v.id) as count
FROM vehicles v
JOIN recordings r ON v.recording_id = r.id
GROUP BY hour ORDER BY hour;

-- Vehicles by class
SELECT vehicle_class, COUNT(*), AVG(speed_kmh)
FROM vehicles
GROUP BY vehicle_class;
```
