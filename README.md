# Traffic Analyser

Analyses vehicle traffic from MotionEye recordings. Detects, tracks, and
measures speed of vehicles across configurable road zones using YOLOv8 (day)
or colour-based light detection (night).

Live dashboard: **https://your-domain.com** *(optional — via Cloudflare Tunnel)*

## Roadmap

- [ ] Multi-user support — allow remote workers to submit results to a central database without needing local network access, reducing setup complexity
- [ ] Speed calibration — investigate and correct the systematic underestimate in speed calculations
- [ ] Improve failed job recovery — more robust reclaiming of jobs that time out or crash mid-processing
- [ ] Settings control panel — UI for tuning motion detection and tracking parameters without editing config files directly

---

## Architecture

```
IP Camera
    │
    │  RTSP stream
    ▼
Raspberry Pi
    │
    │  stream
    ▼
NAS — MotionEye Docker (port 8765)
    │  records to <NAS_RECORDINGS>/Camera1/YYYY-MM-DD/HH-MM-SS.mp4
    │
    ├──► web_ui.py (port 5002)  ◄──  Worker machines poll for jobs
    │         job queue in MariaDB         │
    │                                      │  download + analyse
    │                                      ▼
    │                               worker.py (Mac/PC on LAN)
    │                                      │
    │                                      │  results
    │                                      ▼
    └──────────────────────────────────► MariaDB (port 3307)
                                              │
                                              ▼
                                        dashboard.py (port 5003)
                                              │
                                              ▼
                                   Cloudflare Tunnel (optional)
                                       your-domain.com
```

The NAS is only used for recording and storage — it is too slow to run analysis
at high traffic volumes. Worker machines (any Mac or PC on the local network)
poll `web_ui.py` for jobs, download the file, run the analysis locally, and
post results back to MariaDB. Multiple workers can run simultaneously without
processing the same file twice.

---

## Infrastructure

| Component | Detail |
|---|---|
| NAS address | `<NAS_IP>`, SSH port `<SSH_PORT>` |
| Project path | `/volume1/traffic/traffic_analyser` *(Synology default — adjust for your NAS)* |
| Python venv | `/volume1/traffic/traffic_venv` |
| Recordings | `/volume1/traffic/recordings/Camera1/YYYY-MM-DD/HH-MM-SS.mp4` |
| Annotated output | `/volume1/traffic/annotated` |
| MariaDB | port `3307`, user `<DB_USER>`, password `<DB_PASSWORD>` |
| Web UI (admin) | port `5002` |
| Dashboard | port `5003` |
| Pi camera stream | `http://<PI_IP>:8080/` |
| MotionEye Docker | port `8765` |
| Git repo | `github.com/jon2w/traffic-analyser` |

---

## Key files

```
traffic-analyser/
├── worker.py           Distributed worker — polls web_ui for jobs, runs on Mac/PC
├── analyse.py          Processes a single video file
├── batch.py            Bulk processes the recordings folder directly (single machine)
├── web_ui.py           Admin interface + job queue API (port 5002)
├── dashboard.py        Read-only public dashboard (port 5003)
├── database.py         MariaDB interface, schema setup, job queue functions
├── tracker.py          Centroid tracker + vehicle tracker
├── config.py           All tunable parameters
├── zones_loader.py     Loads zone polygon definitions from zones.json
├── zones.json          Zone polygon definitions (edit via tune_zones.py)
├── tune_zones.py       Interactive zone polygon editor
├── watchdog.sh         NAS cron script — keeps web_ui.py and batch.py running
├── run_batch.sh        Simple batch launcher (activates venv, runs batch.py)
├── sync_from_pi.sh     (Legacy) NAS pulls recordings from Pi via rsync
├── sync_to_nas.sh      (Legacy) Pi pushes recordings to NAS via rsync
├── requirements.txt
└── detect/
    ├── __init__.py     YOLOv8 detection (day mode)
    └── night.py        Colour light detection (night mode)
```

---

## Setting up a worker machine (Windows)

This is the normal way to contribute processing capacity. The worker polls the
NAS for unprocessed recordings, analyses them locally, and posts results back.

### Step 1 — Install Python 3.11

> **Important:** Python **3.11** is required. Python 3.12+ is incompatible with
> PyTorch and will not work.

1. Go to https://www.python.org/downloads/ and download **Python 3.11.x**
   (scroll past the latest version to find 3.11 in the "Looking for a specific
   release?" section).
2. Run the installer. On the first screen, tick **"Add Python to PATH"** before
   clicking Install Now.
3. Open a new Command Prompt (Start → type `cmd` → Enter) and verify:
   ```
   python --version
   ```
   You should see `Python 3.11.x`.

### Step 2 — Get the code

If you have Git installed:
```
git clone https://github.com/jon2w/traffic-analyser.git
cd traffic-analyser
```

Or download and extract the ZIP from GitHub, then open a Command Prompt in
the extracted folder.

### Step 3 — Create a virtual environment

A virtual environment keeps the project's dependencies separate from your
system Python installation. You only do this once.

```
python -m venv venv
```

This creates a `venv` folder inside the project directory.

### Step 4 — Activate the virtual environment

You need to do this **every time** you open a new Command Prompt to work on
this project.

```
venv\Scripts\activate
```

Your prompt should change to show `(venv)` at the start. If Windows blocks
this with a script execution error, run this first:
```
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
then try the activate command again.

### Step 5 — Install dependencies

With the venv active:
```
pip install -r requirements.txt
pip install "numpy<2"
```

> **Note:** NumPy 2.x is incompatible with this project — the second command
> ensures an older version is installed even if the first one pulls in a newer one.

### Step 6 — Start the worker

```
python worker.py --server http://<NAS_IP>:5002
```

The worker will poll the server for jobs and process them continuously. To stop
it press `Ctrl+C`.

**Other useful flags:**
```
python worker.py --server http://<NAS_IP>:5002 --once      # process one job and exit
python worker.py --server http://<NAS_IP>:5002 --dry-run   # show jobs without processing
python worker.py --server http://<NAS_IP>:5002 --day       # force day mode
python worker.py --server http://<NAS_IP>:5002 --night     # force night mode
python worker.py --server http://<NAS_IP>:5002 --poll 30   # poll every 30 seconds
```

### Convenience script (Windows)

A `start_worker.bat` file can be used to start the worker without typing the
full command each time. Create it in the project root:

```bat
cd /d %~dp0
call venv\Scripts\activate
python worker.py --server http://<NAS_IP>:5002
```

Double-click it to start the worker.

### Known issues (Windows)

**NNPACK errors** — if you see errors mentioning `nnpack`, open
`detect/yolo_day.py` and wrap the `torch.backends.nnpack.enabled = False` lines
(around lines 9 and 28) like this:
```python
if hasattr(torch.backends, 'nnpack'):
    torch.backends.nnpack.enabled = False
```

---

## Setting up a worker machine (Mac)

### Step 1 — Install Python 3.11

Using Homebrew (recommended):
```bash
brew install python@3.11
```

Or download from https://www.python.org/downloads/ as above.

### Step 2 — Get the code

```bash
git clone https://github.com/jon2w/traffic-analyser.git
cd traffic-analyser
```

### Step 3 — Create and activate the virtual environment

```bash
python3.11 -m venv venv
source venv/bin/activate
```

Your prompt will show `(venv)`. You need to run `source venv/bin/activate`
each time you open a new terminal.

### Step 4 — Install dependencies

```bash
pip install -r requirements.txt
pip install "numpy<2"
```

### Step 5 — Start the worker

```bash
python worker.py --server http://<NAS_IP>:5002
```

---

## NAS — starting and restarting services

SSH into the NAS first:
```bash
ssh -p <SSH_PORT> <username>@<NAS_IP>
```

### Web UI (admin, port 5002)

```bash
pkill -f web_ui.py
nohup /volume1/traffic/traffic_venv/bin/python /volume1/traffic/traffic_analyser/web_ui.py --port 5002 2>/dev/null &
```

### Dashboard (port 5003)

```bash
pkill -f dashboard.py
cd /volume1/traffic/traffic_analyser
source /volume1/traffic/traffic_venv/bin/activate
nohup python dashboard.py --port 5003 2>/dev/null &
```

### Cloudflare tunnel (public URL)

```bash
sudo systemctl restart cloudflared
```

Config: `/etc/cloudflared/config.yml` — update with your own tunnel ID and domain.

### Batch processing on the NAS (slow — use workers instead)

If no worker machines are available, `batch.py` can process files directly on
the NAS, but it will not keep up with high traffic volumes:

```bash
source /volume1/traffic/traffic_venv/bin/activate
cd /volume1/traffic/traffic_analyser
python batch.py
```

---

## Processing a single file

Useful for testing or debugging:

```bash
python analyse.py --input /path/to/recording.mp4 --no-show
python analyse.py --input /path/to/recording.mp4 --show          # display video while processing
python analyse.py --input /path/to/recording.mp4 --output ~/annotated.mov
python analyse.py --input /path/to/recording.mp4 --day           # force day mode
python analyse.py --input /path/to/recording.mp4 --night         # force night mode
python analyse.py --input /path/to/recording.mp4 --save-db       # write results to DB
```

---

## Dashboard

The dashboard runs locally at `http://<NAS_IP>:5003` and can optionally be
exposed publicly via a Cloudflare Tunnel.

**Tabs:** Overview · Daily · Week Comparison · Patterns · Records

**API endpoints:**
`/api/summary` `/api/daily` `/api/hourly` `/api/hourly_by_dow`
`/api/weekday` `/api/weeks` `/api/vehicles` `/api/zones` `/api/speed_distribution`

---

## Configuration (config.py)

```python
DB_HOST      = "127.0.0.1"
DB_PORT      = 3307
DB_NAME      = "traffic"
DB_USER      = "<DB_USER>"
DB_PASSWORD  = "<DB_PASSWORD>"

RECORDINGS_ROOT = "/volume1/traffic/recordings"

PPM_MAIN_LEFT  = 44.0   # pixels per metre, left lane
PPM_MAIN_RIGHT = 33.0   # pixels per metre, right lane (further from camera)

YOLO_MODEL      = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35
YOLO_DEVICE     = "cpu"

MAX_DISAPPEARED_MS      = 1500
MAX_TRACKER_DISTANCE    = 500
MIN_TRACK_FRAMES        = 12
```

---

## Calibration

### Zone polygons

Zones are defined as polygons in `zones.json` using `(x_fraction, y_fraction)`
coordinates. Edit them interactively:

```bash
python tune_zones.py --video /path/to/recording.mp4
# or
python tune_zones.py --frame /path/to/frame.jpg
```

Controls: `1–9` select zone · click to add/remove points · `S` save and quit · `Q` quit without saving

### Pixels per metre (PPM)

1. Record a clip with a parked car of known length (~4.5 m for a typical car)
2. Measure the car's pixel width at the distance of each lane
3. `PPM = pixel_width / car_length_metres`
4. Update `PPM_MAIN_LEFT` and `PPM_MAIN_RIGHT` in `config.py`

### Night ROI

`NIGHT_ROI_TOP` and `NIGHT_ROI_BOTTOM` define the horizontal band where
lights are detected. Set them to exclude background buildings and foreground
bushes. The orange lines in annotated output show the current boundaries.

---

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
