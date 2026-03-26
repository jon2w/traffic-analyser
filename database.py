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

CREATE TABLE IF NOT EXISTS job_locks (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    filename    VARCHAR(512) NOT NULL UNIQUE,
    worker_id   VARCHAR(128),
    locked_at   DATETIME,
    status      ENUM('pending','processing','done','failed') DEFAULT 'pending',
    fail_reason VARCHAR(512),
    retry_count INT DEFAULT 0,
    retry_after DATETIME DEFAULT NULL,
    INDEX idx_status (status),
    INDEX idx_locked_at (locked_at)
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


def migrate_job_locks():
    """Safely add retry_count and retry_after columns if they don't exist yet."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SHOW COLUMNS FROM job_locks LIKE 'retry_count'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE job_locks ADD COLUMN retry_count INT DEFAULT 0"
                )
            cursor.execute("SHOW COLUMNS FROM job_locks LIKE 'retry_after'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE job_locks ADD COLUMN retry_after DATETIME DEFAULT NULL"
                )
    except Exception as e:
        print(f"migrate_job_locks: {e}")


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
    """
    Insert a new recording row. If this filename was previously processed,
    delete the old record first (cascades to vehicles and track_points).
    Returns the new recording id.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        # Delete previous results for this file if they exist
        cursor.execute("DELETE FROM recordings WHERE filename=%s", (filename,))
        cursor.execute("""
            INSERT INTO recordings 
                (filename, camera_name, recorded_at, processed_at,
                 duration_s, frame_width, frame_height, fps, is_night)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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


# ─── Job queue operations ─────────────────────────────────────────────────────

# Locks older than this are considered stale and can be reclaimed
JOB_LOCK_STALE_MINUTES = 15


def job_queue_populate(recordings_root, camera=None):
    """
    Populate job_locks with all unprocessed recordings not already in the queue.
    Safe to call repeatedly — uses INSERT IGNORE for genuinely new files, and
    resets stuck failed jobs (retry_after=NULL, retry_count<3) back to pending.
    Returns count of new or reset jobs.
    """
    import os
    added = 0
    camera_dirs = [camera] if camera else sorted(os.listdir(recordings_root))
    with get_connection() as conn:
        cursor = conn.cursor()
        for cam in camera_dirs:
            cam_path = os.path.join(recordings_root, cam)
            if not os.path.isdir(cam_path):
                continue
            for date_dir in sorted(os.listdir(cam_path)):
                date_path = os.path.join(cam_path, date_dir)
                if not os.path.isdir(date_path):
                    continue
                for fname in sorted(os.listdir(date_path)):
                    if not fname.lower().endswith(".mp4"):
                        continue
                    full = os.path.abspath(os.path.join(date_path, fname))
                    # Skip if already processed in recordings table
                    cursor.execute(
                        "SELECT id FROM recordings WHERE filename=%s AND processed_at IS NOT NULL",
                        (full,)
                    )
                    if cursor.fetchone():
                        continue
                    # Reset stuck failed jobs (never got a retry scheduled)
                    cursor.execute("""
                        UPDATE job_locks
                        SET status='pending', worker_id=NULL, locked_at=NULL,
                            retry_count=0, retry_after=NULL
                        WHERE filename=%s
                          AND status='failed'
                          AND retry_after IS NULL
                          AND retry_count < 3
                    """, (full,))
                    if cursor.rowcount:
                        added += 1
                        continue
                    cursor.execute(
                        "INSERT IGNORE INTO job_locks (filename, status) VALUES (%s, 'pending')",
                        (full,)
                    )
                    if cursor.rowcount:
                        added += 1
    return added


def job_claim_next(worker_id):
    """
    Atomically claim the next pending job for a worker.
    Also reclaims stale 'processing' jobs (locked > JOB_LOCK_STALE_MINUTES ago).
    Returns (job_id, filename) or (None, None) if nothing available.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        # Reclaim stale processing locks
        cursor.execute("""
            UPDATE job_locks
            SET status='pending', worker_id=NULL, locked_at=NULL
            WHERE status='processing'
              AND locked_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
        """, (JOB_LOCK_STALE_MINUTES,))

        # Reclaim failed jobs whose retry_after has passed
        cursor.execute("""
            UPDATE job_locks
            SET status='pending', worker_id=NULL, locked_at=NULL
            WHERE status='failed'
              AND retry_after IS NOT NULL
              AND retry_after <= NOW()
              AND retry_count < 4
        """)

        # Claim next pending job atomically (respect retry_after)
        cursor.execute("""
            UPDATE job_locks
            SET status='processing', worker_id=%s, locked_at=NOW()
            WHERE status='pending'
              AND (retry_after IS NULL OR retry_after <= NOW())
            ORDER BY id
            LIMIT 1
        """, (worker_id,))

        if cursor.rowcount == 0:
            return None, None

        cursor.execute(
            "SELECT id, filename FROM job_locks WHERE worker_id=%s AND status='processing' ORDER BY locked_at DESC LIMIT 1",
            (worker_id,)
        )
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
        return None, None


def job_complete(job_id, worker_id):
    """Mark a job as done."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE job_locks SET status='done' WHERE id=%s AND worker_id=%s",
            (job_id, worker_id)
        )


def job_fail(job_id, worker_id, reason="", retryable=False):
    """Mark a job as failed. If retryable=True, schedule a retry with backoff."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if retryable:
            # Increment retry count and set retry_after with exponential backoff
            cursor.execute(
                "SELECT retry_count FROM job_locks WHERE id=%s", (job_id,)
            )
            row = cursor.fetchone()
            retry_count = (row[0] if row else 0) + 1
            # 2min, 5min, 10min backoff
            delay_minutes = [2, 5, 10][min(retry_count - 1, 2)]
            if retry_count <= 3:
                cursor.execute("""
                    UPDATE job_locks
                    SET status='failed', fail_reason=%s,
                        retry_count=%s,
                        retry_after=DATE_ADD(NOW(), INTERVAL %s MINUTE)
                    WHERE id=%s AND worker_id=%s
                """, (reason[:512], retry_count, delay_minutes, job_id, worker_id))
            else:
                # Exhausted retries — mark permanently failed
                cursor.execute("""
                    UPDATE job_locks
                    SET status='failed', fail_reason=%s, retry_after=NULL
                    WHERE id=%s AND worker_id=%s
                """, (reason[:512], job_id, worker_id))
        else:
            cursor.execute(
                "UPDATE job_locks SET status='failed', fail_reason=%s WHERE id=%s AND worker_id=%s",
                (reason[:512], job_id, worker_id)
            )


def job_release(job_id, worker_id):
    """Release a job back to pending (e.g. dry-run or download failure)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE job_locks SET status='pending', worker_id=NULL, locked_at=NULL WHERE id=%s AND worker_id=%s",
            (job_id, worker_id)
        )


def job_queue_status():
    """Return dict of status counts for the job queue."""
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM job_locks GROUP BY status
        """)
        return {row["status"]: row["count"] for row in cursor.fetchall()}


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup",  action="store_true",
                        help="Create database, user and tables")
    parser.add_argument("--setup-jobs", action="store_true",
                        help="Add job_locks table to existing database")
    parser.add_argument("--root-password", default="",
                        help="MariaDB root password for --setup")
    parser.add_argument("--status", action="store_true",
                        help="Show row counts")
    args = parser.parse_args()

    if args.setup:
        setup(args.root_password)

    if args.setup_jobs:
        print("Adding job_locks table...")
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_locks (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    filename    VARCHAR(512) NOT NULL UNIQUE,
                    worker_id   VARCHAR(128),
                    locked_at   DATETIME,
                    status      ENUM('pending','processing','done','failed') DEFAULT 'pending',
                    fail_reason VARCHAR(512),
                    INDEX idx_status (status),
                    INDEX idx_locked_at (locked_at)
                )
            """)
        print("Done.")

    if args.status:
        with get_connection() as conn:
            cursor = conn.cursor()
            for table in ("recordings", "vehicles", "track_points"):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                print(f"  {table:20s}: {count:,} rows")
