#!/bin/bash

# Fast Keno - Database Maintenance Script
# This script helps manage and maintain the SQLite database

set -e

DB_FILE="game.db"
BACKUP_DIR="backups"
LOG_FILE="maintenance.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Create backup directory if not exists
mkdir -p "$BACKUP_DIR"

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$LOG_FILE"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" | tee -a "$LOG_FILE"
}

check_database_exists() {
    if [ ! -f "$DB_FILE" ]; then
        error "Database file not found: $DB_FILE"
        exit 1
    fi
}

backup_database() {
    log "Creating database backup..."
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/game.db.${TIMESTAMP}.backup"
    
    cp "$DB_FILE" "$BACKUP_FILE"
    log "Backup created: $BACKUP_FILE"
    
    # Keep only last 30 backups
    log "Cleaning old backups (keeping last 30)..."
    ls -t "$BACKUP_DIR"/game.db.*.backup 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
}

check_integrity() {
    log "Checking database integrity..."
    RESULT=$(sqlite3 "$DB_FILE" "PRAGMA integrity_check;")
    
    if [ "$RESULT" = "ok" ]; then
        log "Database integrity: OK"
    else
        error "Database integrity check failed:"
        echo "$RESULT"
        return 1
    fi
}

optimize_database() {
    log "Optimizing database..."
    
    # Analyze tables
    sqlite3 "$DB_FILE" "ANALYZE;" 2>/dev/null
    log "Analysis completed"
    
    # Vacuum to reclaim space
    log "Running VACUUM (this may take a moment)..."
    sqlite3 "$DB_FILE" "VACUUM;" 2>/dev/null
    log "VACUUM completed"
}

show_statistics() {
    log "Database Statistics:"
    echo ""
    sqlite3 "$DB_FILE" << EOF
.headers on
.mode column
.width 30 15
SELECT 'Table' as Category, 'Count' as Value
UNION ALL
SELECT '--- Users ---', ''
UNION ALL
SELECT 'Total Users', (SELECT COUNT(*) FROM users)
UNION ALL
SELECT 'Active Users (7d)', (SELECT COUNT(*) FROM users WHERE last_active > datetime('now', '-7 days'))
UNION ALL
SELECT '--- Bets ---', ''
UNION ALL
SELECT 'Total Bets', (SELECT COUNT(*) FROM bets)
UNION ALL
SELECT 'Pending Bets', (SELECT COUNT(*) FROM bets WHERE status='pending')
UNION ALL
SELECT 'Resolved Bets', (SELECT COUNT(*) FROM bets WHERE status='resolved')
UNION ALL
SELECT 'Total Wagered (ETB)', (SELECT ROUND(SUM(amount), 2) FROM bets)
UNION ALL
SELECT 'Total Paid (ETB)', (SELECT ROUND(SUM(winnings), 2) FROM bets WHERE status='resolved')
UNION ALL
SELECT '--- Payments ---', ''
UNION ALL
SELECT 'Pending Deposits', (SELECT COUNT(*) FROM deposits WHERE status='pending')
UNION ALL
SELECT 'Pending Withdrawals', (SELECT COUNT(*) FROM withdrawals WHERE status='pending')
UNION ALL
SELECT 'Total Deposits (ETB)', (SELECT ROUND(SUM(amount), 2) FROM deposits WHERE status='approved')
UNION ALL
SELECT 'Total Withdrawals (ETB)', (SELECT ROUND(SUM(amount), 2) FROM withdrawals WHERE status='approved')
UNION ALL
SELECT '--- Security ---', ''
UNION ALL
SELECT 'Fraud Alerts (24h)', (SELECT COUNT(*) FROM fraud_logs WHERE created_at > datetime('now', '-1 day'))
UNION ALL
SELECT 'Fraud Alerts (Total)', (SELECT COUNT(*) FROM fraud_logs)
UNION ALL
SELECT '--- Game ---', ''
UNION ALL
SELECT 'Completed Rounds', (SELECT COUNT(*) FROM game_history)
UNION ALL
SELECT 'Current Round', (SELECT MAX(game_round) FROM game_history);
EOF
    echo ""
}

show_database_size() {
    log "Database Size Information:"
    
    if [ -f "$DB_FILE" ]; then
        SIZE=$(du -h "$DB_FILE" | cut -f1)
        LINES=$(wc -c < "$DB_FILE")
        SIZE_MB=$(echo "scale=2; $LINES / 1024 / 1024" | bc)
        
        log "File Size: $SIZE (${SIZE_MB} MB)"
        
        # Check growth
        if [ -f "$BACKUP_DIR/game.db."*.backup ]; then
            OLDEST_BACKUP=$(ls -t "$BACKUP_DIR"/game.db.*.backup | tail -1)
            OLDEST_SIZE=$(du -h "$OLDEST_BACKUP" | cut -f1)
            log "Oldest Backup Size: $OLDEST_SIZE"
        fi
    fi
}

archive_old_data() {
    DAYS=${1:-90}
    log "Archiving data older than $DAYS days..."
    
    # First, create archive backup
    backup_database
    
    # Archive bets
    ARCHIVED=$(sqlite3 "$DB_FILE" << EOF
BEGIN TRANSACTION;

-- Create archive table if not exists
CREATE TABLE IF NOT EXISTS bets_archive AS SELECT * FROM bets WHERE 0;

-- Count records to archive
SELECT COUNT(*) FROM bets WHERE created_at < datetime('now', '-$DAYS days');
EOF
)
    
    log "Archiving $ARCHIVED old bet records..."
    
    sqlite3 "$DB_FILE" << EOF
BEGIN TRANSACTION;

INSERT INTO bets_archive SELECT * FROM bets 
WHERE created_at < datetime('now', '-$DAYS days');

DELETE FROM bets 
WHERE created_at < datetime('now', '-$DAYS days');

COMMIT;
EOF
    
    log "Archive completed. Records archived: $ARCHIVED"
    
    # Optimize after archiving
    optimize_database
}

export_data() {
    FORMAT=${1:-csv}
    log "Exporting database to $FORMAT format..."
    
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    EXPORT_FILE="export_${TIMESTAMP}.${FORMAT}"
    
    if [ "$FORMAT" = "csv" ]; then
        sqlite3 "$DB_FILE" << EOF
.headers on
.mode csv
.output $EXPORT_FILE
SELECT * FROM bets LIMIT 10000;
EOF
    elif [ "$FORMAT" = "json" ]; then
        sqlite3 "$DB_FILE" << EOF
.headers on
.mode json
.output $EXPORT_FILE
SELECT * FROM bets LIMIT 10000;
EOF
    fi
    
    log "Data exported to: $EXPORT_FILE"
}

rebuild_indexes() {
    log "Rebuilding all indexes..."
    sqlite3 "$DB_FILE" "REINDEX;" 2>/dev/null
    log "Index rebuild completed"
}

show_help() {
    cat << EOF
Fast Keno Database Maintenance Script

Usage: $0 [COMMAND]

Commands:
    backup              Create a backup of the database
    integrity           Check database integrity
    optimize            Optimize database (ANALYZE + VACUUM)
    stats               Show database statistics
    size                Show database size
    archive [DAYS]      Archive data older than DAYS (default: 90)
    export [FORMAT]     Export data (csv, json)
    reindex             Rebuild database indexes
    full-check          Run all checks and optimization
    help                Show this help message

Examples:
    $0 backup
    $0 stats
    $0 archive 90
    $0 export csv
    $0 full-check

EOF
}

case "${1:-help}" in
    backup)
        check_database_exists
        backup_database
        ;;
    integrity)
        check_database_exists
        check_integrity
        ;;
    optimize)
        check_database_exists
        backup_database
        optimize_database
        ;;
    stats)
        check_database_exists
        show_statistics
        ;;
    size)
        check_database_exists
        show_database_size
        ;;
    archive)
        check_database_exists
        archive_old_data "$2"
        ;;
    export)
        check_database_exists
        export_data "$2"
        ;;
    reindex)
        check_database_exists
        backup_database
        rebuild_indexes
        ;;
    full-check)
        check_database_exists
        backup_database
        check_integrity
        show_statistics
        show_database_size
        optimize_database
        log "Full check completed!"
        ;;
    help)
        show_help
        ;;
    *)
        error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac

log "Maintenance task completed."
