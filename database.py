"""
database.py — MariaDB interface for traffic analyser.

Setup:
    python database.py --setup      # creates database, user, and tables
    python database.py --status     # shows row counts
"""

import argparse
import os
from datetime import datetime
from contextlib import contextmanager

try:
    import mysql.connector
except ImportError:
    raise ImportError("Run: pip install mysql-connector-python")

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD


# ─── Schema ───────────────────────────────────────────────────────────────────

SETUP_SQL = """
-- Create database
CREATE DATABASE IF NOT EXISTS `{db}` 
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Create user
CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{password}';
GRANT ALL PRIVILEGES ON `{db}`.* TO '{user}'@'%';
FLUSH PRIVILEGES;
""".format(db=DB_NAME, user=DB_USER, password=DB_PASSWORD)

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS recordings (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    filename        VARCHAR(512) NOT NULL UNIQUE,
    camera_name     VARCHAR(64),
    recorded_at     DATETIME,
    processed_at    DATETIME,
    duration_s      FLOAT,
    frame_width     INT,
    frame_height    INT,
    fps             FLOAT,
    is_night        BOOLEAN,
    vehicle_count   INT DEFAULT 0,
    INDEX idx_recorded_at (recorded_at)
);

CREATE TABLE IF NOT EXISTS vehicles (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    recording_id    INT NOT NULL,
    zone            VARCHAR(64),
    direction       ENUM('left','right','toward','away','unknown') DEFAULT 'unknown',
    speed_kmh       FLOAT,
    vehicle_class   VARCHAR(32),        -- car/truck/bus/motorbike/unknown
    confidence      FLOAT,              -- YOLO confidence, NULL for night mode
    track_frames    INT,
    duration_s      FLOAT,
    first_seen_ms   INT,
    last_seen_ms    INT,
    thumbnail_path  VARCHAR(512),
    detected_at     DATETIME,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE,
    INDEX idx_recording_id (recording_id),
    INDEX idx_detected_at  (detected_at),
    INDEX idx_zone         (zone),
    INDEX idx_direction    (direction)
);

CREATE TABLE IF NOT EXISTS track_points (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    vehicle_id  INT NOT NULL,
    timestamp_ms INT,
    x           FLOAT,
    y           FLOAT,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
    INDEX idx_vehicle_id (vehicle_id)
);
"""


# ─── Connection ───────────────────────────────────────────────────────────────

def _connect(database=DB_NAME):
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT,
        database=database,
        user=DB_USER, password=DB_PASSWORD,
        autocommit=False
    )


@contextmanager
def get_connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Setup ────────────────────────────────────────────────────────────────────

def setup(root_password):
    """Create database, user and tables. Requires MariaDB root password."""
    print("Connecting as root to create database and user...")
    conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT,
        user="root", password=root_password,
        autocommit=True
    )
    cursor = conn.cursor()
    for statement in SETUP_SQL.strip().split(";"):
        statement = statement.strip()
        if statement:
            print(f"  {statement[:60]}...")
            cursor.execute(statement)
    conn.close()
    print("Database and user created.")

    print("Creating tables...")
    conn = _connect()
    cursor = conn.cursor()
    for statement in TABLES_SQL.strip().split(";"):
        statement = statement.strip()
        if statement:
            cursor.execute(statement)
    conn.commit()
    conn.close()
    print("Tables created successfully.")


# ─── Recording operations ─────────────────────────────────────────────────────

def insert_recording(filename, camera_name, recorded_at, duration_s,
                     frame_width, frame_height, fps, is_night):
    """Insert a new recording row. Returns the new recording id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recordings 
                (filename, camera_name, recorded_at, processed_at,
                 duration_s, frame_width, frame_height, fps, is_night)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                processed_at = VALUES(processed_at)
        """, (filename, camera_name, recorded_at, datetime.now(),
              duration_s, frame_width, frame_height, fps, is_night))
        return cursor.lastrowid


def update_recording_count(recording_id, count):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE recordings SET vehicle_count=%s WHERE id=%s",
            (count, recording_id)
        )


def is_already_processed(filename):
    """Returns True if this filename has already been processed."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM recordings WHERE filename=%s AND processed_at IS NOT NULL",
            (filename,)
        )
        return cursor.fetchone() is not None


# ─── Vehicle operations ───────────────────────────────────────────────────────

def insert_vehicle(recording_id, zone, direction, speed_kmh, vehicle_class,
                   confidence, track_frames, duration_s,
                   first_seen_ms, last_seen_ms, thumbnail_path, detected_at):
    """Insert a detected vehicle. Returns the new vehicle id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO vehicles
                (recording_id, zone, direction, speed_kmh, vehicle_class,
                 confidence, track_frames, duration_s,
                 first_seen_ms, last_seen_ms, thumbnail_path, detected_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (recording_id, zone, direction, speed_kmh, vehicle_class,
              confidence, track_frames, duration_s,
              first_seen_ms, last_seen_ms, thumbnail_path, detected_at))
        return cursor.lastrowid


def insert_track_points(vehicle_id, points):
    """Insert raw track points. points = list of (timestamp_ms, x, y)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT INTO track_points (vehicle_id, timestamp_ms, x, y) VALUES (%s,%s,%s,%s)",
            [(vehicle_id, ts, x, y) for ts, x, y in points]
        )


# ─── Query helpers ────────────────────────────────────────────────────────────

def get_summary(days=7):
    """Return a summary dict for the last N days."""
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT 
                COUNT(*)                                    AS total_vehicles,
                AVG(speed_kmh)                              AS avg_speed,
                MAX(speed_kmh)                              AS max_speed,
                SUM(direction='left')                       AS going_left,
                SUM(direction='right')                      AS going_right,
                SUM(zone='opposite_road')                   AS opposite_road,
                SUM(zone='main_road')                       AS main_road
            FROM vehicles v
            JOIN recordings r ON v.recording_id = r.id
            WHERE r.recorded_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        return cursor.fetchone()


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup",  action="store_true",
                        help="Create database, user and tables")
    parser.add_argument("--root-password", default="",
                        help="MariaDB root password for --setup")
    parser.add_argument("--status", action="store_true",
                        help="Show row counts")
    args = parser.parse_args()

    if args.setup:
        setup(args.root_password)

    if args.status:
        with get_connection() as conn:
            cursor = conn.cursor()
            for table in ("recordings", "vehicles", "track_points"):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                print(f"  {table:20s}: {count:,} rows")
