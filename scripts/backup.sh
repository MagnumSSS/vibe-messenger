#!/bin/bash
# Backup script for Private Messenger
# Creates timestamped backup of SQLite database and uploads directory

set -e

# Configuration - can be overridden via environment variables
DATA_DIR="${DATA_DIR:-/var/lib/messenger/data}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/messenger/backups}"
DB_NAME="${DB_NAME:-messenger.db}"
RETAIN_COUNT="${RETAIN_COUNT:-7}"

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Generate timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DB="$BACKUP_DIR/messenger-${TIMESTAMP}.db"
BACKUP_TAR="$BACKUP_DIR/uploads-${TIMESTAMP}.tar.gz"

echo "Starting backup at $(date)"
echo "Data directory: $DATA_DIR"
echo "Backup directory: $BACKUP_DIR"

# Backup SQLite database using .backup command (safe online backup)
if [ -f "$DATA_DIR/$DB_NAME" ]; then
    echo "Backing up database..."
    sqlite3 "$DATA_DIR/$DB_NAME" ".backup '$BACKUP_DB'"
    echo "Database backup created: $BACKUP_DB"
else
    echo "ERROR: Database file not found at $DATA_DIR/$DB_NAME"
    exit 1
fi

# Backup uploads directory
if [ -d "$DATA_DIR/uploads" ]; then
    echo "Backing up uploads..."
    tar -czf "$BACKUP_TAR" -C "$DATA_DIR" uploads
    echo "Uploads backup created: $BACKUP_TAR"
else
    echo "WARNING: Uploads directory not found at $DATA_DIR/uploads"
fi

# Remove old backups (keep only RETAIN_COUNT most recent)
echo "Cleaning old backups (keeping last $RETAIN_COUNT)..."
cd "$BACKUP_DIR"
ls -t messenger-*.db 2>/dev/null | tail -n +$((RETAIN_COUNT + 1)) | xargs -r rm --
ls -t uploads-*.tar.gz 2>/dev/null | tail -n +$((RETAIN_COUNT + 1)) | xargs -r rm --

echo "Backup completed successfully at $(date)"
echo ""
echo "To restore on a clean machine:"
echo "  1. Copy $BACKUP_DB to \$DATA_DIR/$DB_NAME"
echo "  2. Extract $BACKUP_TAR to \$DATA_DIR/"
echo "  3. Start the messenger service"
