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
import json
import os
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, send_file

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
import database as db
import auth

try:
    import mysql.connector
except ImportError:
    raise ImportError("Run: pip install mysql-connector-python")

app = Flask(__name__, static_folder="static")

DASHBOARD_DIR    = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_ROOT  = "/volume1/traffic/recordings"
ANNOTATED_ROOT   = "/volume1/traffic/annotated"


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


def _build_filter_sql(date_from, date_to):
    """
    Build SQL WHERE clause and params for common filtering.
    Supports: user, location_name, user_id, location
    
    Returns (where_clause, params_list)
    """
    where_parts = ["r.recorded_at >= %s AND r.recorded_at < %s"]
    params = [date_from, date_to]
    
    # Filter by username or user_id
    user_filter = request.args.get("user") or request.args.get("username")
    if user_filter:
        where_parts.append("u.username = %s")
        params.append(user_filter)
    
    user_id_filter = request.args.get("user_id")
    if user_id_filter:
        where_parts.append("r.user_id = %s")
        params.append(int(user_id_filter))
    
    # Filter by location
    location_filter = request.args.get("location") or request.args.get("location_name")
    if location_filter:
        where_parts.append("r.location_name = %s")
        params.append(location_filter)
    
    # Filter by submission source (local or remote)
    source_filter = request.args.get("source")
    if source_filter:
        where_parts.append("r.submission_source = %s")
        params.append(source_filter)
    
    return " AND ".join(where_parts), params


# ── Static files ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "dashboard.html")

@app.route("/dow")
def dow():
    return send_from_directory(DASHBOARD_DIR, "dow.html")

@app.route("/busiest")
def busiest():
    return send_from_directory(DASHBOARD_DIR, "busiest.html")

@app.route("/api/download")
def api_download():
    """Stream a raw recording file for local download.
    Validates the path is a known recording in the DB rather than relying
    on filesystem path comparisons (which break on Synology due to symlinks)."""
    path = request.args.get("path", "")
    if not path:
        return "Missing path", 400

    # Check the path exists in the recordings table — this is the safety gate
    rows = _query("SELECT filename FROM recordings WHERE filename = %s LIMIT 1", (path,))
    if not rows:
        return "Not found — path not in recordings database", 404

    if not os.path.isfile(path):
        return "Not found — file missing on disk", 404

    return send_file(path, mimetype="video/mp4",
                     as_attachment=True,
                     download_name=os.path.basename(path))


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    date_from, date_to = _parse_dates()
    where_clause, params = _build_filter_sql(date_from, date_to)
    
    row = _query(f"""
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
        LEFT JOIN users u ON r.user_id = u.id
        WHERE {where_clause}
    """, params, one=True)
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

    # Per-hour breakdown per day to detect missing data gaps
    # Only look at daytime hours (6-22) to avoid night lows triggering false positives
    hourly_rows = _query("""
        SELECT
            DATE(r.recorded_at)  AS date,
            HOUR(r.recorded_at)  AS hour,
            COUNT(*)             AS cnt
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
          AND HOUR(r.recorded_at) BETWEEN 8 AND 20
        GROUP BY DATE(r.recorded_at), HOUR(r.recorded_at)
    """, (date_from, date_to))

    # Build per-day hourly map, also tracking day-of-week
    from collections import defaultdict
    import datetime as dt
    day_hours = defaultdict(dict)   # date_str -> {hour: count}
    day_dow   = {}                  # date_str -> weekday 0=Mon
    for hr in hourly_rows:
        date_str = str(hr['date'])
        day_hours[date_str][int(hr['hour'])] = int(hr['cnt'])
        if date_str not in day_dow:
            day_dow[date_str] = hr['date'].weekday() if hasattr(hr['date'], 'weekday') \
                                else dt.date.fromisoformat(date_str).weekday()

    # Calculate expected hourly average per day-of-week (0=Mon … 6=Sun)
    dow_counts = defaultdict(list)
    for date_str, hours in day_hours.items():
        dow = day_dow.get(date_str, 0)
        dow_counts[dow].extend(hours.values())

    dow_avg = {}
    for dow, counts in dow_counts.items():
        dow_avg[dow] = (sum(counts) / len(counts)) if counts else 0

    result = []
    for row in rows:
        date_str = str(row['date'])
        hours    = day_hours.get(date_str, {})
        dow      = day_dow.get(date_str, 0)
        expected = dow_avg.get(dow, 0)
        threshold = expected * 0.30

        # Find daytime hours below threshold for this day-of-week
        missing_hours = []
        for h in range(8, 21):
            cnt = hours.get(h, 0)
            if cnt < threshold:
                missing_hours.append(h)

        # Only flag if 2+ consecutive hours are missing
        has_gap = False
        consecutive = 0
        for h in range(8, 21):
            if h in missing_hours:
                consecutive += 1
                if consecutive >= 2:
                    has_gap = True
                    break
            else:
                consecutive = 0

        d = {k: (str(v) if hasattr(v, 'isoformat') else
                 float(v) if v is not None and hasattr(v, '__float__') else v)
             for k, v in row.items()}
        d['has_gap']      = has_gap
        d['missing_hours'] = missing_hours if has_gap else []
        result.append(d)

    return jsonify(result)


@app.route("/api/hourly")
def api_hourly():
    date_from, date_to = _parse_dates()
    rows = _query("""
        SELECT
            HOUR(r.recorded_at)                         AS hour,
            COUNT(*)                                    AS total,
            COUNT(DISTINCT DATE(r.recorded_at))         AS day_count,
            ROUND(AVG(v.speed_kmh), 1)                  AS avg_speed
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        GROUP BY HOUR(r.recorded_at)
        ORDER BY hour
    """, (date_from, date_to))
    # Fill all 24 hours, divide total by number of days that had data for that hour
    by_hour = {row["hour"]: row for row in rows}
    result = []
    for h in range(24):
        row = by_hour.get(h)
        if row:
            day_count = int(row["day_count"]) or 1
            result.append({
                "hour":      h,
                "total":     round(int(row["total"]) / day_count),
                "avg_speed": row["avg_speed"]
            })
        else:
            result.append({"hour": h, "total": 0, "avg_speed": None})
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

@app.route("/api/busiest_periods")
def api_busiest_periods():
    """
    Returns the top N busiest time windows of a given duration.
    Counts vehicles whose recorded_at falls within each window.
    Windows are anchored to recording start times to avoid splitting real events.

    Query params:
      from, to       — date range (YYYY-MM-DD)
      minutes        — window size in minutes (1,2,5,15,30,60)
      limit          — number of results (default 50)
    """
    date_from, date_to = _parse_dates()
    minutes = max(1, min(60, int(request.args.get("minutes", 60))))
    limit   = max(1, min(200, int(request.args.get("limit", 50))))

    # Pull all vehicles with their recording info in the date range
    rows = _query("""
        SELECT
            v.id                AS vehicle_id,
            r.id                AS recording_id,
            r.filename          AS filename,
            r.recorded_at       AS window_start,
            r.recorded_at       AS rec_start,
            v.first_seen_ms     AS first_seen_ms
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
        ORDER BY r.recorded_at, v.first_seen_ms
    """, (date_from, date_to))

    if not rows:
        return jsonify([])

    from datetime import timedelta

    # Build list of (absolute_timestamp, recording_id, filename)
    events = []
    for row in rows:
        base = row['rec_start']
        offset_ms = row['first_seen_ms'] or 0
        ts = base + timedelta(milliseconds=offset_ms)
        events.append((ts, row['recording_id'], row['filename']))

    events.sort(key=lambda x: x[0])

    # Sliding window count — advance right pointer
    window_td = timedelta(minutes=minutes)
    best = []
    n = len(events)
    left = 0
    for right in range(n):
        ts_right = events[right][0]
        # shrink left until window fits
        while events[left][0] < ts_right - window_td:
            left += 1
        count = right - left + 1
        best.append((count, events[left][0], events[right][0],
                     events[left][2], events[left][1]))

    # Deduplicate overlapping windows — keep highest count per non-overlapping slot
    best.sort(key=lambda x: -x[0])
    results = []
    used_ranges = []
    for count, t_start, t_end, _filename, _rec_id in best:
        # Skip if this window overlaps an already-selected one
        overlaps = any(
            not (t_end < us or t_start > ue)
            for us, ue in used_ranges
        )
        if overlaps:
            continue
        used_ranges.append((t_start, t_end))

        # Collect all distinct recordings whose vehicles fall inside this window
        seen_recs = {}
        for ts, rid, fn in events:
            if t_start <= ts <= t_end + window_td:
                if rid not in seen_recs:
                    seen_recs[rid] = fn
        recordings = [
            {'filename': fn, 'basename': os.path.basename(fn)}
            for rid, fn in sorted(seen_recs.items())
        ]

        results.append({
            'rank':         len(results) + 1,
            'count':        count,
            'window_start': t_start.strftime('%Y-%m-%d %H:%M:%S'),
            'window_end':   t_end.strftime('%Y-%m-%d %H:%M:%S'),
            'recordings':   recordings,
        })
        if len(results) >= limit:
            break

    return jsonify(results)




@app.route("/speeds")
def speeds():
    return send_from_directory(DASHBOARD_DIR, "speeds.html")

@app.route("/api/top_speeds")
def api_top_speeds():
    """Return the top N fastest vehicle records with recording info."""
    date_from, date_to = _parse_dates()
    limit = max(1, min(200, int(request.args.get("limit", 100))))

    rows = _query("""
        SELECT
            v.id            AS vehicle_id,
            v.speed_kmh     AS speed_kmh,
            v.vehicle_class AS vehicle_class,
            v.direction     AS direction,
            v.zone          AS zone,
            v.confidence    AS confidence,
            v.track_frames  AS track_frames,
            v.duration_s    AS duration_s,
            r.id            AS recording_id,
            r.filename      AS filename,
            r.recorded_at   AS recorded_at,
            r.is_night      AS is_night
        FROM vehicles v
        JOIN recordings r ON v.recording_id = r.id
        WHERE r.recorded_at >= %s AND r.recorded_at < %s
          AND v.speed_kmh IS NOT NULL
        ORDER BY v.speed_kmh DESC
        LIMIT %s
    """, (date_from, date_to, limit))

    return jsonify([{
        k: (str(v) if hasattr(v, 'isoformat') else
            float(v) if v is not None and hasattr(v, '__float__') else
            bool(v) if k == 'is_night' else v)
        for k, v in row.items()
    } for row in rows])


@app.route("/api/vehicles/<int:vehicle_id>", methods=["DELETE"])
def api_delete_vehicle(vehicle_id):
    """Delete a single vehicle record by ID."""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM vehicles WHERE id = %s", (vehicle_id,))
            if cur.rowcount == 0:
                return jsonify({"ok": False, "error": "Not found"}), 404
        return jsonify({"ok": True, "deleted": vehicle_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── User and location filtering APIs ───────────────────────────────────────────

@app.route("/api/users")
def api_users():
    """
    Get list of users who have submitted data.
    Returns user_id, username, recording_count, vehicle_count.
    """
    rows = _query("""
        SELECT
            u.id AS user_id,
            u.username,
            u.display_name,
            COUNT(DISTINCT r.id) AS recording_count,
            COUNT(DISTINCT v.id) AS vehicle_count
        FROM users u
        LEFT JOIN recordings r ON u.id = r.user_id
        LEFT JOIN vehicles v ON r.id = v.recording_id
        WHERE u.is_active = TRUE
        GROUP BY u.id, u.username
        ORDER BY vehicle_count DESC
    """)
    return jsonify({"users": rows})


@app.route("/api/locations")
def api_locations():
    """
    Get list of locations with data summary.
    Returns location_name, recording_count, vehicle_count, avg_speed.
    """
    rows = _query("""
        SELECT
            COALESCE(r.location_name, 'Unknown') AS location_name,
            COUNT(DISTINCT r.id) AS recording_count,
            COUNT(DISTINCT v.id) AS vehicle_count,
            ROUND(AVG(v.speed_kmh), 1) AS avg_speed
        FROM recordings r
        LEFT JOIN vehicles v ON r.id = v.recording_id
        WHERE r.location_name IS NOT NULL
        GROUP BY r.location_name
        ORDER BY vehicle_count DESC
    """)
    return jsonify({"locations": [{
        k: (float(v) if v is not None and k == 'avg_speed' and hasattr(v, '__float__') else v)
        for k, v in row.items()
    } for row in rows]})


@app.route("/api/user/<int:user_id>/summary")
def api_user_summary(user_id):
    """
    Get summary statistics for a specific user.
    """
    date_from, date_to = _parse_dates()
    row = _query("""
        SELECT
            COUNT(DISTINCT v.id)                    AS total_vehicles,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            ROUND(MAX(v.speed_kmh), 1)              AS max_speed,
            COUNT(DISTINCT r.id)                    AS recordings_processed,
            MIN(r.recorded_at)                      AS first_submission,
            MAX(r.recorded_at)                      AS last_submission
        FROM recordings r
        LEFT JOIN vehicles v ON r.id = v.recording_id
        WHERE r.user_id = %s
          AND r.recorded_at >= %s AND r.recorded_at < %s
    """, (user_id, date_from, date_to), one=True)
    
    if row and row.get('total_vehicles'):
        return jsonify({k: (
            str(v) if hasattr(v, 'isoformat') else
            float(v) if v is not None and hasattr(v, '__float__') else v
        ) for k, v in row.items()})
    return jsonify({"error": "No data for user"}), 404


@app.route("/api/location/<location_name>/summary")
def api_location_summary(location_name):
    """
    Get summary statistics for a specific location.
    """
    date_from, date_to = _parse_dates()
    row = _query("""
        SELECT
            COUNT(DISTINCT v.id)                    AS total_vehicles,
            ROUND(AVG(v.speed_kmh), 1)              AS avg_speed,
            ROUND(MAX(v.speed_kmh), 1)              AS max_speed,
            COUNT(DISTINCT r.id)                    AS recordings_processed,
            COUNT(DISTINCT r.user_id)               AS unique_users
        FROM recordings r
        LEFT JOIN vehicles v ON r.id = v.recording_id
        WHERE r.location_name = %s
          AND r.recorded_at >= %s AND r.recorded_at < %s
    """, (location_name, date_from, date_to), one=True)
    
    if row and row.get('total_vehicles'):
        return jsonify({k: (float(v) if v is not None and hasattr(v, '__float__') else v) 
                       for k, v in row.items()})
    return jsonify({"error": "No data for location"}), 404


@app.route("/api/submit_results", methods=["POST"])
@auth.require_auth
def api_submit_results(user):
    """Accept analysis results from a remote user running analysis locally."""
    data = request.json or {}

    required_fields = ["filename", "vehicles"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    try:
        filename       = data["filename"]
        location_name  = data.get("location_name", "Unknown")[:128]
        camera_name    = data.get("camera_name", f"user_{user['username']}")[:64]
        recorded_at_str = data.get("recorded_at")
        duration_s     = float(data["duration_s"]) if data.get("duration_s") is not None else 0.0
        frame_width    = int(data["frame_width"]) if data.get("frame_width") is not None else 0
        frame_height   = int(data["frame_height"]) if data.get("frame_height") is not None else 0
        fps            = float(data["fps"]) if data.get("fps") is not None else 0.0
        is_night       = bool(data.get("is_night", False))
        vehicles_data  = data.get("vehicles", [])

        if recorded_at_str:
            try:
                recorded_at = datetime.fromisoformat(recorded_at_str)
            except Exception:
                recorded_at = datetime.now()
        else:
            recorded_at = datetime.now()

        # Use just the basename as the stored filename (no disk write needed here)
        basename = os.path.splitext(os.path.basename(filename))[0]
        safe_name = re.sub(r"[^\w\-]", "_", basename)[:64]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored_filename = f"remote_{user['id']}_{safe_name}_{ts}"

        recording_id = db.insert_recording(
            filename=stored_filename,
            camera_name=camera_name,
            recorded_at=recorded_at,
            duration_s=duration_s,
            frame_width=frame_width,
            frame_height=frame_height,
            fps=fps,
            is_night=is_night,
            user_id=user["id"],
            location_name=location_name,
            submission_source="remote",
        )

        vehicle_count = 0
        for v in vehicles_data:
            try:
                vehicle_id = db.insert_vehicle(
                    recording_id=recording_id,
                    zone=v.get("zone", "unknown"),
                    direction=v.get("direction", "unknown"),
                    speed_kmh=float(v["speed_kmh"]) if v.get("speed_kmh") is not None else None,
                    vehicle_class=v.get("vehicle_class", "unknown"),
                    confidence=float(v["confidence"]) if v.get("confidence") is not None else None,
                    track_frames=int(v.get("track_frames", 0)),
                    duration_s=float(v["duration_s"]) if v.get("duration_s") is not None else None,
                    first_seen_ms=int(v.get("first_seen_ms", 0)),
                    last_seen_ms=int(v.get("last_seen_ms", 0)),
                    thumbnail_path=v.get("thumbnail_path"),
                    detected_at=datetime.now(),
                )
                track_points = v.get("track_points", [])
                if track_points:
                    db.insert_track_points(vehicle_id, track_points)
                vehicle_count += 1
            except Exception as e:
                print(f"Warning: failed to insert vehicle: {e}")

        db.update_recording_count(recording_id, vehicle_count)

        return jsonify({
            "ok": True,
            "recording_id": recording_id,
            "vehicle_count": vehicle_count,
            "message": f"Results submitted successfully — {vehicle_count} vehicle(s) recorded",
        }), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Traffic Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

