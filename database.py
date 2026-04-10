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
CREATE TABLE IF NOT EXISTS users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(128) NOT NULL UNIQUE,
    api_key         VARCHAR(64) NOT NULL UNIQUE,
    display_name    VARCHAR(128),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_active       BOOLEAN DEFAULT TRUE,
    is_admin        BOOLEAN DEFAULT FALSE,
    submission_type ENUM('local','remote') DEFAULT 'remote',
    INDEX idx_api_key (api_key),
    INDEX idx_is_active (is_active)
);

CREATE TABLE IF NOT EXISTS recordings (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    filename        VARCHAR(512) NOT NULL UNIQUE,
    user_id         INT,
    camera_name     VARCHAR(64),
    location_name   VARCHAR(128),
    recorded_at     DATETIME,
    submitted_at    DATETIME,
    processed_at    DATETIME,
    duration_s      FLOAT,
    frame_width     INT,
    frame_height    INT,
    fps             FLOAT,
    is_night        BOOLEAN,
    vehicle_count   INT DEFAULT 0,
    submission_source ENUM('local','remote') DEFAULT 'local',
    INDEX idx_user_id (user_id),
    INDEX idx_recorded_at (recorded_at),
    INDEX idx_submitted_at (submitted_at),
    INDEX idx_location_name (location_name),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
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


def migrate_multi_user_support():
    """Safely add multi-user support columns and tables."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            # Add user_id, location_name, submitted_at, submission_source columns to recordings if they don't exist
            cursor.execute("SHOW COLUMNS FROM recordings LIKE 'user_id'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE recordings ADD COLUMN user_id INT"
                )
                cursor.execute(
                    "ALTER TABLE recordings ADD FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
                )
            
            cursor.execute("SHOW COLUMNS FROM recordings LIKE 'location_name'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE recordings ADD COLUMN location_name VARCHAR(128)"
                )
                cursor.execute(
                    "ALTER TABLE recordings ADD INDEX idx_location_name (location_name)"
                )
            
            cursor.execute("SHOW COLUMNS FROM recordings LIKE 'submitted_at'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE recordings ADD COLUMN submitted_at DATETIME"
                )
                cursor.execute(
                    "ALTER TABLE recordings ADD INDEX idx_submitted_at (submitted_at)"
                )
            
            cursor.execute("SHOW COLUMNS FROM recordings LIKE 'submission_source'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE recordings ADD COLUMN submission_source ENUM('local','remote') DEFAULT 'local'"
                )
            
            # Create localhost user for local submissions if not exists
            cursor.execute("SELECT id FROM users WHERE username='localhost'")
            if not cursor.fetchone():
                import secrets
                api_key = secrets.token_hex(32)
                cursor.execute("""
                    INSERT INTO users (username, api_key, display_name, is_admin, submission_type)
                    VALUES ('localhost', %s, 'Local Submissions', TRUE, 'local')
                """, (api_key,))
                conn.commit()
                print(f"Created 'localhost' user with API key: {api_key}")
            
    except Exception as e:
        print(f"migrate_multi_user_support: {e}")


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
                     frame_width, frame_height, fps, is_night,
                     user_id=None, location_name=None, submission_source="local"):
    """
    Insert a new recording row. If this filename was previously processed,
    delete the old record first (cascades to vehicles and track_points).
    Returns the new recording id.
    """
    submitted_at = datetime.now() if submission_source == "remote" else None
    
    with get_connection() as conn:
        cursor = conn.cursor()
        # Delete previous results for this file if they exist
        cursor.execute("DELETE FROM recordings WHERE filename=%s", (filename,))
        cursor.execute("""
            INSERT INTO recordings 
                (filename, user_id, camera_name, location_name, recorded_at, submitted_at, 
                 processed_at, duration_s, frame_width, frame_height, fps, is_night, submission_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (filename, user_id, camera_name, location_name, recorded_at, submitted_at, 
              datetime.now(), duration_s, frame_width, frame_height, fps, is_night, submission_source))
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

        # Reclaim failed jobs with no retry scheduled (non-permanent failures,
        # i.e. retry_count < 3 — permanent failures are marked with retry_count=3)
        cursor.execute("""
            UPDATE job_locks
            SET status='pending', worker_id=NULL, locked_at=NULL
            WHERE status='failed'
              AND retry_after IS NULL
              AND retry_count < 3
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
            # Permanent failure — set retry_count=3 so it is not auto-reclaimed
            cursor.execute(
                "UPDATE job_locks SET status='failed', fail_reason=%s, retry_count=3 WHERE id=%s AND worker_id=%s",
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


# ─── User operations ──────────────────────────────────────────────────────────

def create_user(username, display_name, is_admin=False, submission_type='remote'):
    """
    Create a new user with an auto-generated API key.
    Returns (user_id, api_key) or (None, None) if user already exists.
    """
    import secrets
    api_key = secrets.token_hex(32)
    
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (username, api_key, display_name, is_admin, submission_type)
                VALUES (%s, %s, %s, %s, %s)
            """, (username, api_key, display_name or username, is_admin, submission_type))
            return cursor.lastrowid, api_key
    except:
        return None, None


def validate_api_key(api_key):
    """
    Validate an API key. Returns user dict (including user_id, username, is_admin) or None.
    """
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, username, display_name, is_admin, submission_type
            FROM users WHERE api_key=%s AND is_active=TRUE
        """, (api_key,))
        return cursor.fetchone()


def get_user_by_id(user_id):
    """Get user information by ID."""
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, username, display_name, is_admin, submission_type, is_active
            FROM users WHERE id=%s
        """, (user_id,))
        return cursor.fetchone()


def get_localhost_user():
    """Get the special 'localhost' user for local submissions."""
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, username, api_key FROM users WHERE username='localhost'
        """)
        return cursor.fetchone()


def list_users():
    """List all active users."""
    with get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, username, display_name, is_admin, submission_type, created_at
            FROM users WHERE is_active=TRUE
            ORDER BY created_at DESC
        """)
        return cursor.fetchall()


def deactivate_user(user_id):
    """Deactivate a user (soft delete)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_active=FALSE WHERE id=%s",
            (user_id,)
        )


def regenerate_api_key(user_id):
    """Generate a new API key for a user. Returns the new key."""
    import secrets
    new_key = secrets.token_hex(32)
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET api_key=%s WHERE id=%s",
            (new_key, user_id)
        )
    return new_key


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
    parser.add_argument("--create-user", metavar="USERNAME",
                        help="Create a user and print their API key")
    parser.add_argument("--display-name", default=None,
                        help="Display name for --create-user")
    parser.add_argument("--admin", action="store_true",
                        help="Make the new user an admin (use with --create-user)")
    parser.add_argument("--list-users", action="store_true",
                        help="List all users")
    parser.add_argument("--regenerate-key", metavar="USERNAME",
                        help="Regenerate API key for a user")
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

    if args.create_user:
        user_id, api_key = create_user(
            username=args.create_user,
            display_name=args.display_name or args.create_user,
            is_admin=args.admin,
        )
        if user_id:
            print(f"  User     : {args.create_user}")
            print(f"  User ID  : {user_id}")
            print(f"  Admin    : {args.admin}")
            print(f"  API key  : {api_key}")
            print()
            print("  Save the API key — it cannot be retrieved later.")
        else:
            print(f"  Error: user '{args.create_user}' already exists.")

    if args.list_users:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, display_name, is_admin, is_active, submission_type, created_at FROM users ORDER BY id"
            )
            rows = cursor.fetchall()
        if not rows:
            print("  No users found.")
        else:
            print(f"  {'ID':<5} {'Username':<20} {'Display name':<25} {'Admin':<6} {'Active':<7} {'Type':<8} Created")
            print(f"  {'-'*90}")
            for row in rows:
                print(f"  {row[0]:<5} {row[1]:<20} {row[2]:<25} {'yes' if row[3] else 'no':<6} {'yes' if row[4] else 'no':<7} {row[5]:<8} {row[6]}")

    if args.regenerate_key:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE username=%s", (args.regenerate_key,))
            row = cursor.fetchone()
        if not row:
            print(f"  Error: user '{args.regenerate_key}' not found.")
        else:
            new_key = regenerate_api_key(row[0])
            print(f"  User    : {args.regenerate_key}")
            print(f"  New key : {new_key}")
            print()
            print("  Save the API key — it cannot be retrieved later.")
