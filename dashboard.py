"""
dashboard.py — Read-only traffic dashboard API.

Run:
    python dashboard.py --port 5003

Serves:
    GET  /                          → dashboard.html
    GET  /api/summary?from=&to=     → totals for date range
    GET  /api/daily?from=&to=       → per-day breakdown
    GET  /api/hourly?from=&to=      → per-hour breakdown
    GET  /api/weekday?from=&to=     → by day-of-week
    GET  /api/weeks?from=&to=       → week-by-week comparison
    GET  /api/vehicles?from=&to=&page=&per_page=&zone=&direction=
    GET  /api/zones                 → list of distinct zones
"""

import argparse
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

try:
    import mysql.connector
except ImportError:
    raise ImportError("Run: pip install mysql-connector-python")

app = Flask(__name__, static_folder="static")

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))


def _connect():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASSWORD, autocommit=True
    )


def _query(sql, params=(), one=False):
    conn = _connect()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params)
    result = cursor.fetchone() if one else cursor.fetchall()
    conn.close()
    return result


def _parse_dates():
    """Parse from/to query params, defaulting to last 7 days."""
    to_str   = request.args.get("to",   datetime.now().strftime("%Y-%m-%d"))
    from_str = request.args.get("from", (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d"))
    try:
        date_from = datetime.strptime(from_str, "%Y-%m-%d")
        date_to   = datetime.strptime(to_str,   "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        date_from = datetime.now() - timedelta(days=7)
        date_to   = datetime.now()
    return date_from, date_to


# ── Static files ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "dashboard.html")

@app.route("/dow")
def dow():
    return send_from_directory(DASHBOARD_DIR, "dow.html")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    date_from, date_to = _parse_dates()
    row = _query("""
        SELECT
            COUNT(*)                                AS total_vehicles,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            ROUND(MAX(v.speed_kmh), 1)              AS max_speed,
            SUM(v.direction = 'left')               AS going_left,
            SUM(v.direction = 'right')              AS going_right,
            COUNT(DISTINCT DATE(r.recorded_at))     AS days_with_data,
            COUNT(DISTINCT r.id)                    AS recordings_processed,
            SUM(r.is_night)                         AS night_recordings
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
    """, (date_from, date_to), one=True)
    # Convert Decimal/None safely
    return jsonify({k: (float(v) if v is not None else None) for k, v in row.items()})


@app.route("/api/daily")
def api_daily():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            DATE(r.recorded_at)                     AS date,
            COUNT(*)                                AS total,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            ROUND(MAX(v.speed_kmh), 1)              AS max_speed,
            SUM(v.direction = 'left')               AS going_left,
            SUM(v.direction = 'right')              AS going_right,
            SUM(r.is_night = 0)                     AS day_count,
            SUM(r.is_night = 1)                     AS night_count
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY DATE(r.recorded_at)
        ORDER BY date
    """, (date_from, date_to))
    return jsonify([{k: (str(v) if hasattr(v, 'isoformat') else
                        float(v) if v is not None and hasattr(v, '__float__') else v)
                    for k, v in row.items()} for row in rows])


@app.route("/api/hourly")
def api_hourly():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            HOUR(r.recorded_at)                     AS hour,
            COUNT(*)                                AS total,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY HOUR(r.recorded_at)
        ORDER BY hour
    """, (date_from, date_to))
    # Fill all 24 hours
    by_hour = {row["hour"]: row for row in rows}
    result = []
    for h in range(24):
        result.append(by_hour.get(h, {"hour": h, "total": 0, "avg_speed": None}))
    return jsonify(result)


@app.route("/api/weekday")
def api_weekday():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            DAYOFWEEK(r.recorded_at)                AS dow,
            DAYNAME(r.recorded_at)                  AS day_name,
            COUNT(*)                                AS total,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            COUNT(DISTINCT DATE(r.recorded_at))     AS num_days
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY DAYOFWEEK(r.recorded_at), DAYNAME(r.recorded_at)
        ORDER BY dow
    """, (date_from, date_to))
    return jsonify([{k: (float(v) if v is not None and hasattr(v, '__float__') else v)
                    for k, v in row.items()} for row in rows])


@app.route("/api/weeks")
def api_weeks():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            YEARWEEK(r.recorded_at, 1)              AS week_key,
            MIN(DATE(r.recorded_at))                AS week_start,
            COUNT(*)                                AS total,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            ROUND(MAX(v.speed_kmh), 1)              AS max_speed,
            SUM(v.direction = 'left')               AS going_left,
            SUM(v.direction = 'right')              AS going_right
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY YEARWEEK(r.recorded_at, 1)
        ORDER BY week_key
    """, (date_from, date_to))
    return jsonify([{k: (str(v) if hasattr(v, 'isoformat') else
                        float(v) if v is not None and hasattr(v, '__float__') else v)
                    for k, v in row.items()} for row in rows])


@app.route("/api/vehicles")
def api_vehicles():
    date_from, date_to = _parse_dates()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 50)))
    zone      = request.args.get("zone", "")
    direction = request.args.get("direction", "")
    offset   = (page - 1) * per_page

    filters = "r.recorded_at >= %s AND r.recorded_at < %s"
    params  = [date_from, date_to]
    if zone:
        filters += " AND v.zone = %s"
        params.append(zone)
    if direction:
        filters += " AND v.direction = %s"
        params.append(direction)

    total = _query(f"""
        SELECT COUNT(*) AS n FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE {filters}
    """, params, one=True)["n"]

    rows = _query(f"""
        SELECT
            v.id, v.zone, v.direction, v.speed_kmh, v.vehicle_class,
            v.confidence, v.track_frames, v.duration_s,
            v.first_seen_ms, v.last_seen_ms,
            r.recorded_at, r.filename, r.is_night
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE {filters}
        ORDER BY r.recorded_at DESC, v.id DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "vehicles": [{k: (str(v) if hasattr(v, 'isoformat') else
                         float(v) if v is not None and hasattr(v, '__float__') else v)
                     for k, v in row.items()} for row in rows]
    })


@app.route("/api/zones")
def api_zones():
    rows = _query("SELECT DISTINCT zone FROM vehicles WHERE zone IS NOT NULL ORDER BY zone")
    return jsonify([r["zone"] for r in rows])


@app.route("/api/speed_distribution")
def api_speed_distribution():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            FLOOR(v.speed_kmh / 10) * 10            AS bucket,
            COUNT(*)                                AS count
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
          AND v.speed_kmh IS NOT NULL AND v.speed_kmh > 0
        GROUP BY bucket
        ORDER BY bucket
    """, (date_from, date_to))
    return jsonify([{k: int(v) if v is not None else 0 for k, v in row.items()} for row in rows])




@app.route("/api/hourly_by_dow")
def api_hourly_by_dow():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            DAYOFWEEK(r.recorded_at)                        AS dow,
            DAYNAME(r.recorded_at)                          AS day_name,
            HOUR(r.recorded_at)                             AS hour,
            COUNT(*)                                        AS total,
            COUNT(DISTINCT DATE(r.recorded_at))             AS day_count
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY DAYOFWEEK(r.recorded_at), DAYNAME(r.recorded_at), HOUR(r.recorded_at)
        ORDER BY dow, hour
    """, (date_from, date_to))

    from collections import defaultdict
    by_dow = defaultdict(lambda: {'day_name': '', 'hours': [0]*24})
    for row in rows:
        dow  = int(row['dow'])
        hour = int(row['hour'])
        by_dow[dow]['day_name'] = row['day_name']
        day_count = int(row['day_count']) or 1
        by_dow[dow]['hours'][hour] = round(int(row['total']) / day_count)

    result = []
    for dow in [2,3,4,5,6,7,1]:  # Mon-Sun (DAYOFWEEK: 1=Sun, 2=Mon ... 7=Sat)
        if dow in by_dow:
            result.append({
                'dow': dow,
                'day_name': by_dow[dow]['day_name'],
                'hours': by_dow[dow]['hours']
            })
    return jsonify(result)

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Traffic Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

