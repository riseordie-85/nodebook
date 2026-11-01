#!/bin/bash
# Nightly backup of Nodebook's database and media to Google Drive via rclone.
#
# Runs on the NUC HOST itself (not inside the Docker container) - the paths
# below are the same bind-mounted folders the container reads and writes,
# so this works directly off disk with no need to exec into the container
# for anything.
#
# One-time setup required before this will work - see the setup notes
# that came with this script.
set -euo pipefail

APP_DIR="/mnt/ext-storage/AI/comfyui-tracker"
DATA_DIR="$APP_DIR/data"
SNAPSHOT_DIR="$APP_DIR/backup-snapshot"
RCLONE_REMOTE="gdrive:NodebookBackup"
LOG_FILE="$APP_DIR/backup.log"
DATE_STAMP=$(date +%Y-%m-%d)

echo "=== Backup started: $(date) ===" >> "$LOG_FILE"

LIVE_DB="$DATA_DIR/instance/tracker.db"

# SQLite silently CREATES a new, empty database if you point it at a path
# that doesn't exist, rather than failing - so without this check, a
# missing or moved database would produce an empty "backup" and still
# report success, which is worse than no backup at all because it hides
# the problem instead of surfacing it.
if [ ! -f "$LIVE_DB" ]; then
    echo "ERROR: database not found at $LIVE_DB - aborting, nothing was backed up." >> "$LOG_FILE"
    exit 1
fi

mkdir -p "$SNAPSHOT_DIR"
SNAPSHOT_FILE="$SNAPSHOT_DIR/tracker-$DATE_STAMP.db"
rm -f "$SNAPSHOT_FILE"

# A safe, consistent snapshot of the live database. VACUUM INTO is built
# for exactly this - it works correctly even while the app container has
# this same file open and is actively writing to it, unlike a plain `cp`
# which could grab a half-written file mid-write and silently produce a
# corrupted backup that only reveals itself the day you actually need it.
sqlite3 "$LIVE_DB" "VACUUM INTO '$SNAPSHOT_FILE';" >> "$LOG_FILE" 2>&1

# Extra safety net: confirm the snapshot itself is actually valid before
# trusting it enough to upload anywhere.
INTEGRITY=$(sqlite3 "$SNAPSHOT_FILE" "PRAGMA integrity_check;")
if [ "$INTEGRITY" != "ok" ]; then
    echo "ERROR: snapshot failed integrity check ($INTEGRITY) - aborting, not uploading a bad backup." >> "$LOG_FILE"
    exit 1
fi

# .env holds SECRET_KEY, the login password hash, and the API key - losing
# it means losing access to everything else, so it's worth backing up too.
# Keep the Drive destination folder private, since it holds these.
cp "$APP_DIR/.env" "$SNAPSHOT_DIR/.env"

# `copy`, not `sync`, everywhere below - deliberately. Sync would delete
# files from the Drive backup the moment they're deleted on the NUC, which
# defeats the point of a backup if that deletion was ever a mistake. Copy
# only ever adds or updates, so the backup still has yesterday's files
# even if today goes wrong.
rclone copy "$SNAPSHOT_DIR" "$RCLONE_REMOTE/snapshots" --log-file "$LOG_FILE" --log-level INFO
rclone copy "$DATA_DIR/uploads" "$RCLONE_REMOTE/uploads" --log-file "$LOG_FILE" --log-level INFO
rclone copy "$DATA_DIR/thumbnails" "$RCLONE_REMOTE/thumbnails" --log-file "$LOG_FILE" --log-level INFO
rclone copy "$DATA_DIR/reference_uploads" "$RCLONE_REMOTE/reference_uploads" --log-file "$LOG_FILE" --log-level INFO
rclone copy "$DATA_DIR/reference_thumbnails" "$RCLONE_REMOTE/reference_thumbnails" --log-file "$LOG_FILE" --log-level INFO

echo "=== Backup finished: $(date) ===" >> "$LOG_FILE"
