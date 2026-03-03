#!/bin/bash
# sync_to_nas.sh — Sync MotionEye recordings to NAS
#
# Install:
#   1. Copy this file to /home/jon/sync_to_nas.sh
#   2. chmod +x /home/jon/sync_to_nas.sh
#   3. Add to crontab: crontab -e
#      */5 * * * * /home/jon/sync_to_nas.sh >> /home/jon/sync.log 2>&1
#
# ── Configuration ────────────────────────────────────────────────────────────
SOURCE="/var/lib/motioneye/Camera1/"
NAS_HOST="192.168.1.99"
NAS_PORT="4362"                         # SSH port on NAS
NAS_USER="traffic_sync"
NAS_PATH="/volume1/traffic/recordings/"
RETAIN_DAYS=1                           # Delete local files older than this
# ── Sync ─────────────────────────────────────────────────────────────────────
rsync \
    --recursive \
    --links \
    --times \
    --compress \
    --ignore-errors \
    --no-perms \
    --no-owner \
    --no-group \
    --min-size=1k \
    --exclude="*.thumb" \
    --exclude="*.jpg" \
    -e "ssh -p ${NAS_PORT}" \
    "$SOURCE" \
    "${NAS_USER}@${NAS_HOST}:${NAS_PATH}"

RSYNC_EXIT=$?
if [ $RSYNC_EXIT -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Sync OK"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') Sync FAILED (exit code $RSYNC_EXIT) — skipping cleanup"
    exit 1
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────
# Only runs if sync succeeded, so we don't delete files that didn't transfer
DELETED=$(find "$SOURCE" -type f -mtime +${RETAIN_DAYS} \( -name "*.mp4" -o -name "*.avi" \) -print -delete)
if [ -n "$DELETED" ]; then
    COUNT=$(echo "$DELETED" | wc -l)
    echo "$(date '+%Y-%m-%d %H:%M:%S') Cleanup: removed $COUNT file(s) older than ${RETAIN_DAYS} day(s)"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') Cleanup: nothing to remove"
fi