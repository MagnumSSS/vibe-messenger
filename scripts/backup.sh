#!/bin/bash
# Backup script for Private Messenger
#
# Phase R2: вся логика перенесена в scripts/backup.py — онлайн-копия через
# sqlite3.Connection.backup() (Online Backup API, WAL-safe, без остановки сервиса)
# плюс обязательный PRAGMA integrity_check ПО КОПИИ: битую копию скрипт не считает
# бэкапом и завершается ненулевым кодом.
#
# Шим оставлен, чтобы не ломать cron и документацию: те же env-переменные
# (DATA_DIR, BACKUP_DIR, DB_NAME, RETAIN_COUNT) и тот же exit-код.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

exec "$PY" "$HERE/backup.py" "$@"
