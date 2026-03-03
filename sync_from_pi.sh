#!/bin/bash
# sync_from_pi.sh — Pull MotionEye recordings from Pi to NAS
#
# Install on NAS:
#   1. Copy to /volume1/traffic/sync_from_pi.sh
#   2. chmod +x /volume1/traffic/sync_from_pi.sh
#   3. Add to crontab: crontab -e
#      */5 * * * * /volume1/traffic/sync_from_pi.sh >> /volume1/traffic/sync_from_pi.log 2>&1
#
# -- Configuration -------------------------------------------------------------
PI_HOST="192.168.1.224"
PI_USER="jon"
PI_SSH_KEY="/var/services/homes/jon/.ssh/id_ed25519"
PI_BASE="/var/lib/motioneye"        # Base path on Pi — one subdir per camera

NAS_BASE="/volume1/traffic/recordings"  # Base path on NAS

CAMERAS=("Camera1")                 # Add more cameras here e.g. ("Camera1" "Camera2")
# -- Sync ----------------------------------------------------------------------
for CAMERA in "${CAMERAS[@]}"; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') Syncing $CAMERA..."

    mkdir -p "${NAS_BASE}/${CAMERA}"

    rsync -r --times \
        --no-perms --no-owner --no-group \
        --ignore-errors \
        --min-size=1k \
        --exclude="*.thumb" \
        --exclude="*.jpg" \
        -e "ssh -p 22 -i ${PI_SSH_KEY}" \
        "${PI_USER}@${PI_HOST}:${PI_BASE}/${CAMERA}/" \
        "${NAS_BASE}/${CAMERA}/"

    if [ $? -eq 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') $CAMERA sync OK"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') $CAMERA sync FAILED — skipping cleanup"
        continue
    fi



done