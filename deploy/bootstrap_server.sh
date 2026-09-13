#!/usr/bin/env bash
#
# Phase 7.5a: подготовка чистой Debian 12 под ВайбБункер (vibebunker.ru).
#
# Скрипт ИДЕМПОТЕНТЕН: его можно гонять повторно, повторный прогон ничего не ломает
# и печатает «уже сделано» вместо повторной установки.
#
# Что делает:
#   1. проверяет, что запущен от root на Debian 12;
#   2. таймзона Europe/Moscow, локаль-независимо;
#   3. пакеты: git, python3.11 + venv, nginx, certbot, ufw, fail2ban, curl, rsync;
#   4. swap 1G, если RAM < 2G и свопа ещё нет;
#   5. пользователь vibebunker + sudo (пароль не задаётся — вход только по ключу);
#   6. ufw: deny incoming, allow 22/80/443, enable;
#   7. fail2ban: джейл sshd (backend systemd), enable + start;
#   8. sshd: PermitRootLogin no, PasswordAuthentication no, PermitEmptyPasswords no —
#      ТОЛЬКО если у vibebunker уже есть непустой ~/.ssh/authorized_keys.
#      Нет ключа → шаг 8 пропускается с громким предупреждением (чтобы не запереть себя снаружи).
#
# Чего скрипт НЕ делает намеренно:
#   * не меняет пароль root (это делается руками: см. docs/DEPLOY.md, шаг 1);
#   * не копирует SSH-ключи (их заливает пользователь с КЛИЕНТА: ssh-copy-id);
#   * не клонирует репозиторий, не создаёт venv, не трогает .env и сертификаты.
#
# Использование:
#   bash deploy/bootstrap_server.sh              # обычный прогон
#   FORCE_SSH_LOCKDOWN=1 bash deploy/bootstrap_server.sh   # закрыть пароли, даже если ключа нет (ОПАСНО)
#   APP_USER=vibebunker bash deploy/bootstrap_server.sh    # другое имя пользователя
#
set -euo pipefail

APP_USER="${APP_USER:-vibebunker}"
APP_DIR_NAME="${APP_DIR_NAME:-vibe_mes}"
TIMEZONE="${TIMEZONE:-Europe/Moscow}"
SWAP_FILE="${SWAP_FILE:-/swapfile}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-1024}"
SSH_PORT="${SSH_PORT:-22}"
FORCE_SSH_LOCKDOWN="${FORCE_SSH_LOCKDOWN:-0}"

STEP=0
log()  { printf '\n\033[1m[%s] %s\033[0m\n' "$((++STEP))" "$*"; }
ok()   { printf '    ok   %s\n' "$*"; }
skip() { printf '    skip %s\n' "$*"; }
warn() { printf '    \033[33mВНИМАНИЕ: %s\033[0m\n' "$*" >&2; }
die()  { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Предполётные проверки
# --------------------------------------------------------------------------- #
log "Предполётные проверки"
[ "$(id -u)" -eq 0 ] || die "запускать от root: sudo bash deploy/bootstrap_server.sh"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    ok "система: ${PRETTY_NAME:-неизвестно}"
    case "${ID:-}:${VERSION_ID:-}" in
        debian:12*) : ;;
        *) warn "скрипт писан под Debian 12; продолжаю, но проверяй имена пакетов" ;;
    esac
else
    warn "/etc/os-release не читается — определить дистрибутив не могу"
fi

export DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------- #
# 2. Таймзона
# --------------------------------------------------------------------------- #
log "Таймзона ${TIMEZONE}"
current_tz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo '')"
if [ "$current_tz" = "$TIMEZONE" ]; then
    skip "таймзона уже ${TIMEZONE}"
else
    timedatectl set-timezone "$TIMEZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/${TIMEZONE}" /etc/localtime
    ok "таймзона: $(date '+%Z %z')"
fi

# --------------------------------------------------------------------------- #
# 3. Пакеты
# --------------------------------------------------------------------------- #
log "Пакеты (apt)"
PACKAGES=(
    ca-certificates curl git rsync sudo unattended-upgrades
    python3 python3-venv python3-pip
    nginx certbot python3-certbot-nginx
    ufw fail2ban
)
missing=()
for pkg in "${PACKAGES[@]}"; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
        continue
    fi
    missing+=("$pkg")
done
if [ "${#missing[@]}" -eq 0 ]; then
    skip "все пакеты уже стоят (${#PACKAGES[@]} шт.)"
else
    ok "ставлю: ${missing[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing[@]}"
fi
# В Debian 12 python3 == 3.11; убеждаемся, что версия не ниже 3.11
py_ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
ok "python3 ${py_ver}"
python3 - <<'PY' || die "нужен Python >= 3.11 (в Debian 12 это системный python3)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

# --------------------------------------------------------------------------- #
# 4. Swap 1G, если RAM < 2G
# --------------------------------------------------------------------------- #
log "Swap"
ram_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
ok "RAM: ${ram_mb} MB"
active_swap_kb="$(awk '/SwapTotal/ {print $2}' /proc/meminfo)"
if [ "$ram_mb" -ge 2048 ]; then
    skip "RAM >= 2G — swap не нужен"
elif [ "${active_swap_kb:-0}" -gt 0 ]; then
    skip "swap уже активен ($((active_swap_kb / 1024)) MB)"
elif [ -f "$SWAP_FILE" ]; then
    swapon "$SWAP_FILE" 2>/dev/null || true
    ok "существующий ${SWAP_FILE} подключён"
else
    fallocate -l "${SWAP_SIZE_MB}M" "$SWAP_FILE" 2>/dev/null \
        || dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MB" status=none
    chmod 600 "$SWAP_FILE"
    mkswap -q "$SWAP_FILE" >/dev/null
    swapon "$SWAP_FILE"
    grep -q "^${SWAP_FILE} " /etc/fstab || echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
    ok "создан swap ${SWAP_SIZE_MB} MB (${SWAP_FILE}), прописан в /etc/fstab"
fi

# --------------------------------------------------------------------------- #
# 5. Пользователь приложения
# --------------------------------------------------------------------------- #
log "Пользователь ${APP_USER}"
if id -u "$APP_USER" >/dev/null 2>&1; then
    skip "пользователь ${APP_USER} уже есть"
else
    adduser --disabled-password --gecos "" "$APP_USER" >/dev/null
    ok "создан ${APP_USER} (пароль не задан — вход только по ключу)"
fi
if id -nG "$APP_USER" | tr ' ' '\n' | grep -qx sudo; then
    skip "${APP_USER} уже в группе sudo"
else
    usermod -aG sudo "$APP_USER"
    ok "${APP_USER} добавлен в sudo"
fi
USER_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "${USER_HOME}/.ssh"
touch "${USER_HOME}/.ssh/authorized_keys"
chmod 600 "${USER_HOME}/.ssh/authorized_keys"
chown "$APP_USER:$APP_USER" "${USER_HOME}/.ssh/authorized_keys"
ok "каталог ключей: ${USER_HOME}/.ssh (700), authorized_keys (600)"
install -d -m 755 -o "$APP_USER" -g "$APP_USER" "${USER_HOME}/${APP_DIR_NAME}" 2>/dev/null || true

# Перенос ключей root → vibebunker при первом прогоне (типичный случай Aeza: ключ залит root'у)
keys_count="$(grep -cE '^(ssh|ecdsa|sk-)' "${USER_HOME}/.ssh/authorized_keys" 2>/dev/null || true)"
keys_count="${keys_count:-0}"
if [ "$keys_count" -eq 0 ] && [ -s /root/.ssh/authorized_keys ]; then
    cat /root/.ssh/authorized_keys >> "${USER_HOME}/.ssh/authorized_keys"
    chown "$APP_USER:$APP_USER" "${USER_HOME}/.ssh/authorized_keys"
    keys_count="$(grep -cE '^(ssh|ecdsa|sk-)' "${USER_HOME}/.ssh/authorized_keys" || true)"
    keys_count="${keys_count:-0}"
    ok "ключи root скопированы пользователю ${APP_USER} (${keys_count} шт.)"
fi
ok "ключей у ${APP_USER}: ${keys_count}"

# --------------------------------------------------------------------------- #
# 6. ufw
# --------------------------------------------------------------------------- #
log "Файрвол ufw (22/80/443)"
ufw --force default deny incoming >/dev/null
ufw --force default allow outgoing >/dev/null
for port in "$SSH_PORT" 80 443; do
    ufw allow "${port}/tcp" >/dev/null
done
if ufw status | head -1 | grep -q "active"; then
    skip "ufw уже включён — правила обновлены"
else
    ufw --force enable >/dev/null
    ok "ufw включён"
fi
ufw status numbered | sed 's/^/    /'

# --------------------------------------------------------------------------- #
# 7. fail2ban (sshd)
# --------------------------------------------------------------------------- #
log "fail2ban: джейл sshd"
JAIL=/etc/fail2ban/jail.d/sshd.local
NEW_JAIL="$(cat <<EOF
# Phase 7.5a — управляется deploy/bootstrap_server.sh
[sshd]
enabled  = true
port     = ${SSH_PORT}
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
)"
if [ -f "$JAIL" ] && [ "$(cat "$JAIL")" = "$NEW_JAIL" ]; then
    skip "${JAIL} уже актуален"
else
    printf '%s\n' "$NEW_JAIL" > "$JAIL"
    ok "записан ${JAIL}"
fi
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban
sleep 1
fail2ban-client status sshd 2>/dev/null | sed 's/^/    /' || warn "fail2ban поднялся, но статус sshd пока пуст"

# --------------------------------------------------------------------------- #
# 8. sshd lockdown — только при наличии ключа
# --------------------------------------------------------------------------- #
log "SSH lockdown (root-вход и пароли — запретить)"
SSHD_DROPIN=/etc/ssh/sshd_config.d/99-vibebunker.conf
if [ "$keys_count" -eq 0 ] && [ "$FORCE_SSH_LOCKDOWN" != "1" ]; then
    warn "у ${APP_USER} нет SSH-ключа — пароли НЕ отключены, иначе ты запрёшь себя снаружи."
    warn "С клиента выполни:  ssh-copy-id ${APP_USER}@<IP>   затем прогони скрипт ещё раз."
else
    mkdir -p /etc/ssh/sshd_config.d
    cat > "$SSHD_DROPIN" <<EOF
# Phase 7.5a — управляется deploy/bootstrap_server.sh
Port ${SSH_PORT}
PermitRootLogin no
PasswordAuthentication no
PermitEmptyPasswords no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
PubkeyAuthentication yes
MaxAuthTries 4
LoginGraceTime 30
EOF
    # В Debian 12 основной sshd_config может явно разрешать пароли — гасим строки-конкуренты
    if grep -qE '^\s*(PasswordAuthentication|PermitRootLogin)\s' /etc/ssh/sshd_config; then
        sed -i -E 's/^\s*(PasswordAuthentication|PermitRootLogin)\s/#&/' /etc/ssh/sshd_config
        ok "закомментированы конкурирующие строки в /etc/ssh/sshd_config"
    fi
    if sshd -t; then
        systemctl reload ssh 2>/dev/null || systemctl reload sshd
        ok "sshd перечитан; активная политика:"
        sshd -T 2>/dev/null | grep -iE '^(permitrootlogin|passwordauthentication|permitemptypasswords|port) ' | sed 's/^/      /'
    else
        rm -f "$SSHD_DROPIN"
        die "sshd -t не прошёл — дропин удалён, конфиг не применён"
    fi
fi

# --------------------------------------------------------------------------- #
# Итог
# --------------------------------------------------------------------------- #
cat <<EOF

--------------------------------------------------------------------
BOOTSTRAP OK
  пользователь : ${APP_USER} (sudo), каталог приложения: ${USER_HOME}/${APP_DIR_NAME}
  ключей SSH   : ${keys_count}
  файрвол      : ufw allow ${SSH_PORT}/80/443, остальное deny
  fail2ban     : sshd, maxretry=5, bantime=1h
  sshd         : $( [ "$keys_count" -gt 0 ] || [ "$FORCE_SSH_LOCKDOWN" = "1" ] && echo "пароли и root-вход ЗАКРЫТЫ" || echo "НЕ тронут (нет ключа)" )

Дальше — docs/DEPLOY.md, шаг 4: перелогиниться ключом и проверить,
что вход по паролю отбивается:  ssh -o PubkeyAuthentication=no ${APP_USER}@<IP>
--------------------------------------------------------------------
EOF
