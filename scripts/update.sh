#!/bin/bash
#
# Phase R3: обновление с автооткатом.
#
# Порядок:
#   1. PRE-CHECK  — selftest на текущем коде; красный → апдейт запрещён (нельзя обновлять сломанное).
#   2. PREV_HASH  — запомнить HEAD; снять бэкап БД через scripts/backup.py.
#   3. FETCH      — git fetch + reset --hard origin/$UPDATE_BRANCH.
#   4. RESTART    — systemctl (если включён deploy/messenger.service) или dev-режим (pkill + nohup).
#   5. POST-CHECK — /health + selftest.
#   6. ЗЕЛЁНО     — data/last_good_hash = новый HEAD, "UPDATE OK", exit 0.
#      КРАСНО     — откат: reset --hard PREV_HASH, БД из бэкапа, рестарт, "ROLLBACK DONE", exit 1.
#
# --dry-run: печатает план и ничего не меняет.
#
# Env (все можно переопределить):
#   UPDATE_BRANCH  — ветка обновления (default: текущая)
#   DATA_DIR       — default /var/lib/messenger/data
#   BACKUP_DIR     — default /var/lib/messenger/backups
#   RETAIN_COUNT   — сколько копий хранить (default 5)
#   APP_HOST/PORT  — dev-режим: куда биндить uvicorn (default 0.0.0.0:8000)
#   HEALTH_URL     — default http://127.0.0.1:$APP_PORT/health
#   PYTHON         — интерпретатор с зависимостями (default python3)
#
# Exit-коды:
#   0 — UPDATE OK (или dry-run)
#   1 — откат выполнен (ROLLBACK DONE)
#   2 — PRE-CHECK красный, апдейт запрещён
#   3 — не снялся бэкап
#   4 — не удалось fetch/reset

set -uo pipefail  # без -e: ошибки обрабатываем сами, иначе откат не выполнится

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 4

# --------------------------------------------------------------------------- #
# Micro update-env: .env в окружение ДО чтения любых настроек.
#
# Боевые DATA_DIR/BACKUP_DIR живут в .env. Раньше update.sh их не читал, и
# scripts/backup.py брал дефолты /var/lib/messenger/*, где БД нет — бэкап падал,
# а без бэкапа обновление запрещено, так что апдейт вставал намертво.
#
# `set -a` помечает всё присвоенное на экспорт, поэтому переменные видят и
# дочерние процессы (backup.py, selftest.py, log_event.py). Значения, уже
# заданные в шелле, восстанавливаются после подгрузки файла: явный запуск
# вида `BACKUP_DIR=/tmp/x bash scripts/update.sh` важнее .env — тот же контракт,
# что у scripts/load_env.py.
# --------------------------------------------------------------------------- #
if [ -f .env ]; then
    # запоминаем то, что пришло из шелла (пустое значение = переменная не задана)
    _PRE_DATA_DIR="${DATA_DIR:-}"
    _PRE_BACKUP_DIR="${BACKUP_DIR:-}"
    _PRE_RETAIN_COUNT="${RETAIN_COUNT:-}"
    _PRE_DB_NAME="${DB_NAME:-}"
    _PRE_SECRET_KEY="${SECRET_KEY:-}"
    _PRE_APP_MODE="${APP_MODE:-}"
    _PRE_APP_HOST="${APP_HOST:-}"
    _PRE_APP_PORT="${APP_PORT:-}"
    _PRE_MAX_UPLOAD_BYTES="${MAX_UPLOAD_BYTES:-}"
    _PRE_FIRST_USER_ADMIN="${FIRST_USER_ADMIN:-}"
    _PRE_PYTHON="${PYTHON:-}"
    _PRE_UPDATE_BRANCH="${UPDATE_BRANCH:-}"
    _PRE_HEALTH_URL="${HEALTH_URL:-}"

    set -a
    # shellcheck disable=SC1091  # путь известен только на целевой машине
    . ./.env
    set +a

    # шелл побеждает файл
    [ -n "$_PRE_DATA_DIR" ]         && export DATA_DIR="$_PRE_DATA_DIR"
    [ -n "$_PRE_BACKUP_DIR" ]       && export BACKUP_DIR="$_PRE_BACKUP_DIR"
    [ -n "$_PRE_RETAIN_COUNT" ]     && export RETAIN_COUNT="$_PRE_RETAIN_COUNT"
    [ -n "$_PRE_DB_NAME" ]          && export DB_NAME="$_PRE_DB_NAME"
    [ -n "$_PRE_SECRET_KEY" ]       && export SECRET_KEY="$_PRE_SECRET_KEY"
    [ -n "$_PRE_APP_MODE" ]         && export APP_MODE="$_PRE_APP_MODE"
    [ -n "$_PRE_APP_HOST" ]         && export APP_HOST="$_PRE_APP_HOST"
    [ -n "$_PRE_APP_PORT" ]         && export APP_PORT="$_PRE_APP_PORT"
    [ -n "$_PRE_MAX_UPLOAD_BYTES" ] && export MAX_UPLOAD_BYTES="$_PRE_MAX_UPLOAD_BYTES"
    [ -n "$_PRE_FIRST_USER_ADMIN" ] && export FIRST_USER_ADMIN="$_PRE_FIRST_USER_ADMIN"
    [ -n "$_PRE_PYTHON" ]           && export PYTHON="$_PRE_PYTHON"
    [ -n "$_PRE_UPDATE_BRANCH" ]    && export UPDATE_BRANCH="$_PRE_UPDATE_BRANCH"
    [ -n "$_PRE_HEALTH_URL" ]       && export HEALTH_URL="$_PRE_HEALTH_URL"
    unset _PRE_DATA_DIR _PRE_BACKUP_DIR _PRE_RETAIN_COUNT _PRE_DB_NAME \
          _PRE_SECRET_KEY _PRE_APP_MODE _PRE_APP_HOST _PRE_APP_PORT \
          _PRE_MAX_UPLOAD_BYTES _PRE_FIRST_USER_ADMIN _PRE_PYTHON \
          _PRE_UPDATE_BRANCH _PRE_HEALTH_URL
    ENV_FILE_LOADED=1
else
    ENV_FILE_LOADED=0
fi

# Micro update-env [3]: интерпретатор пинуется на venv репозитория — там лежат
# зависимости. Системный python3 — только запасной вариант.
PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

DATA_DIR="${DATA_DIR:-/var/lib/messenger/data}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/messenger/backups}"
RETAIN_COUNT="${RETAIN_COUNT:-5}"
DB_NAME="${DB_NAME:-messenger.db}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${APP_PORT}/health}"
DEV_LOG="${DATA_DIR}/dev_server.log"
LAST_GOOD="${DATA_DIR}/last_good_hash"

# Micro update-env: значения после слияния «.env + шелл» уезжают дочерним процессам.
# backup.py и selftest.py читают именно их, а не дефолты /var/lib/messenger/*.
export DATA_DIR BACKUP_DIR RETAIN_COUNT DB_NAME APP_HOST APP_PORT HEALTH_URL

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
fi

log()  { echo "[update] $*"; }
warn() { echo "[update] ВНИМАНИЕ: $*" >&2; }

UPDATE_BRANCH="${UPDATE_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null)}"
# dev-режим: убиваем только свой инстанс (по хосту и порту), чужие серверы не трогаем
DEV_PATTERN="uvicorn main:app --host ${APP_HOST} --port ${APP_PORT}"

# --------------------------------------------------------------------------- #
# Сервис: systemd или dev-режим
# --------------------------------------------------------------------------- #
service_mode() {
    if command -v systemctl >/dev/null 2>&1; then
        if [ -f /etc/systemd/system/messenger.service ] || [ -f /lib/systemd/system/messenger.service ]; then
            if [ "$(systemctl is-enabled messenger 2>/dev/null)" = "enabled" ]; then
                echo "systemd"
                return
            fi
        fi
    fi
    echo "dev"
}
SERVICE_MODE="$(service_mode)"

health_ok() {
    "$PY" - "$HEALTH_URL" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

wait_health() {
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if health_ok; then return 0; fi
        sleep 1
    done
    return 1
}

wait_port_free() {
    local deadline=$((SECONDS + 15))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! "$PY" - "$APP_PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.3)
busy = s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0
s.close()
sys.exit(0 if busy else 1)
PY
        then return 0; fi
        sleep 0.5
    done
    return 1
}

stop_app() {
    if [ "$SERVICE_MODE" = "systemd" ]; then
        systemctl stop messenger || true
    else
        pkill -f "$DEV_PATTERN" >/dev/null 2>&1 || true
        wait_port_free || true
    fi
}

start_app() {
    if [ "$SERVICE_MODE" = "systemd" ]; then
        systemctl start messenger || true
    else
        mkdir -p "$DATA_DIR"
        # те же env, что у ручного запуска; логи — в DATA_DIR
        SECRET_KEY="${SECRET_KEY:-dev-secret-key-change-in-production}" \
        FIRST_USER_ADMIN="${FIRST_USER_ADMIN:-1}" \
        MAX_UPLOAD_BYTES="${MAX_UPLOAD_BYTES:-10485760}" \
        DATA_DIR="$DATA_DIR" \
        PORT="$APP_PORT" \
            setsid nohup "$PY" -m uvicorn main:app --host "$APP_HOST" --port "$APP_PORT" \
                >> "$DEV_LOG" 2>&1 < /dev/null &
        disown >/dev/null 2>&1 || true
    fi
    wait_health
}

restart_app() {
    stop_app
    start_app
}

# события обновления пишем в общий data/logs/app.log (без секретов в тексте)
log_event() {
    if [ -f scripts/log_event.py ]; then
        DATA_DIR="$DATA_DIR" "$PY" scripts/log_event.py "$1" "$2" >/dev/null 2>&1 || true
    fi
}

run_selftest() {
    # selftest поднимает свой инстанс на 8099 во временном DATA_DIR:
    # рабочий сервер он не трогает
    "$PY" scripts/selftest.py
    return $?
}

# --------------------------------------------------------------------------- #
# PLAN (--dry-run)
# --------------------------------------------------------------------------- #
PREV_HASH="$(git rev-parse HEAD 2>/dev/null || echo 'unknown')"

if [ "$DRY_RUN" = "1" ]; then
    echo "==================== UPDATE DRY-RUN ===================="
    echo "репозиторий:     $REPO_ROOT"
    if [ "$ENV_FILE_LOADED" = "1" ]; then
        echo ".env:            загружен и экспортирован дочерним процессам"
    else
        echo ".env:            не найден — работают дефолты и переменные шелла"
    fi
    echo "интерпретатор:   $PY"
    echo "ветка:           $UPDATE_BRANCH"
    echo "текущий HEAD:    $PREV_HASH"
    echo "режим сервиса:   $SERVICE_MODE"
    if [ "$SERVICE_MODE" = "systemd" ]; then
        echo "перезапуск:      systemctl restart messenger"
    else
        echo "перезапуск:      pkill -f '$DEV_PATTERN' && nohup uvicorn (лог: $DEV_LOG)"
    fi
    echo "бэкап:           DATA_DIR=$DATA_DIR BACKUP_DIR=$BACKUP_DIR RETAIN_COUNT=$RETAIN_COUNT $PY scripts/backup.py"
    echo "health:          $HEALTH_URL"
    echo "проверки:        $PY scripts/selftest.py (до и после)"
    echo "при успехе:      $LAST_GOOD <- новый HEAD"
    echo "при провале:     git reset --hard $PREV_HASH + БД из бэкапа + рестарт"
    echo "========================================================"
    echo "Ничего не изменено (dry-run)."
    exit 0
fi

echo "==================== UPDATE ===================="
if [ "$ENV_FILE_LOADED" = "1" ]; then
    log ".env загружен: DATA_DIR=$DATA_DIR BACKUP_DIR=$BACKUP_DIR"
else
    log ".env не найден: DATA_DIR=$DATA_DIR BACKUP_DIR=$BACKUP_DIR (дефолты/шелл)"
fi
log "интерпретатор: $PY"
log "ветка: $UPDATE_BRANCH, HEAD: $PREV_HASH, режим: $SERVICE_MODE"

# --------------------------------------------------------------------------- #
# 1. PRE-CHECK: на сломанном не обновляемся
# --------------------------------------------------------------------------- #
log "PRE-CHECK: selftest до обновления"
if run_selftest | tee /tmp/update_selftest_pre.log | tail -3; then
    log "PRE-CHECK зелёный — обновляться можно"
    log_event "update" "PRE-CHECK selftest: OK, обновляемся с $PREV_HASH"
else
    warn "PRE-CHECK красный: на сломанном состоянии обновление запрещено"
    log_event "update" "PRE-CHECK selftest: FAIL, обновление запрещено (HEAD=$PREV_HASH)"
    echo "UPDATE ABORTED: pre-check failed"
    exit 2
fi

# --------------------------------------------------------------------------- #
# 2. Бэкап
# --------------------------------------------------------------------------- #
log "бэкап БД → $BACKUP_DIR"
BACKUP_OUT="$(mktemp)"
if ! DATA_DIR="$DATA_DIR" BACKUP_DIR="$BACKUP_DIR" RETAIN_COUNT="$RETAIN_COUNT" \
        "$PY" scripts/backup.py > "$BACKUP_OUT" 2>&1; then
    cat "$BACKUP_OUT"
    warn "бэкап не снялся — обновление отменено"
    log_event "update" "бэкап не снялся, обновление отменено"
    echo "UPDATE ABORTED: backup failed"
    rm -f "$BACKUP_OUT"
    exit 3
fi
cat "$BACKUP_OUT"
BACKUP_FILE="$(sed -n 's/^BACKUP_DB=//p' "$BACKUP_OUT" | tail -1)"
rm -f "$BACKUP_OUT"
if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    warn "не удалось определить файл бэкапа — обновление отменено"
    echo "UPDATE ABORTED: backup file not found"
    exit 3
fi
log "бэкап: $BACKUP_FILE"

# --------------------------------------------------------------------------- #
# 3. FETCH + RESET
# --------------------------------------------------------------------------- #
log "git fetch && git reset --hard origin/$UPDATE_BRANCH"
if ! git fetch origin 2>&1; then
    warn "git fetch не удался"
    echo "UPDATE ABORTED: fetch failed"
    exit 4
fi
if ! git reset --hard "origin/$UPDATE_BRANCH" 2>&1; then
    warn "git reset --hard origin/$UPDATE_BRANCH не удался"
    echo "UPDATE ABORTED: reset failed"
    exit 4
fi
NEW_HASH="$(git rev-parse HEAD)"
log "код обновлён: $PREV_HASH → $NEW_HASH"
log_event "update" "код обновлён: $PREV_HASH → $NEW_HASH"

# --------------------------------------------------------------------------- #
# 4. RESTART
# --------------------------------------------------------------------------- #
log "перезапуск сервиса ($SERVICE_MODE)"
if ! restart_app; then
    warn "сервис не поднялся после обновления"
    RESTART_FAILED=1
else
    RESTART_FAILED=0
    log "/health отвечает"
fi

# --------------------------------------------------------------------------- #
# 5. POST-CHECK
# --------------------------------------------------------------------------- #
POST_OK=1
if [ "$RESTART_FAILED" = "0" ]; then
    log "POST-CHECK: selftest после обновления"
    if run_selftest | tee /tmp/update_selftest_post.log | tail -3; then
        POST_OK=0
    fi
fi

# --------------------------------------------------------------------------- #
# 6. Итог
# --------------------------------------------------------------------------- #
if [ "$POST_OK" = "0" ]; then
    mkdir -p "$DATA_DIR"
    echo "$NEW_HASH" > "$LAST_GOOD"
    log "last_good_hash → $LAST_GOOD ($NEW_HASH)"
    log "предыдущий HEAD (для ручного отката): $PREV_HASH"
    log_event "update" "UPDATE OK: $PREV_HASH → $NEW_HASH"
    echo "UPDATE OK: $PREV_HASH → $NEW_HASH"
    exit 0
fi

# -------- ROLLBACK --------
warn "POST-CHECK красный — откатываемся на $PREV_HASH"
stop_app
git reset --hard "$PREV_HASH"
log "код откатан: $(git rev-parse HEAD)"

DB_PATH="$DATA_DIR/$DB_NAME"
if [ -f "$BACKUP_FILE" ]; then
    # WAL-файлы от новой версии к старой БД не относятся — убираем их до подкладывания копии
    rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
    cp "$BACKUP_FILE" "$DB_PATH"
    log "БД восстановлена из $BACKUP_FILE"
else
    warn "файл бэкапа потерян — БД не восстановлена!"
fi

if restart_app; then
    log "/health после отката отвечает"
else
    warn "после отката сервис не поднялся — нужен ручной разбор"
fi
log_event "update" "ROLLBACK DONE: HEAD=$(git rev-parse HEAD), БД из $BACKUP_FILE"
echo "ROLLBACK DONE: HEAD=$(git rev-parse HEAD), БД из $BACKUP_FILE"
exit 1
