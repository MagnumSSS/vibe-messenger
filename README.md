# Private Messenger - Phase 5.1 Final

A lightweight private web messenger for small groups, designed for Raspberry Pi 3B+.

## Features

- User registration and login with session-based authentication
- First user gets admin role (configurable via `FIRST_USER_ADMIN`)
- One-on-one text messaging
- Real-time message delivery via WebSocket
- Offline message storage in SQLite
- File attachments with chunked streaming (no RAM overload)
- Invite-only registration
- Admin panel: warns, bans, unban, admin promotion
- Block users双向
- Profile with avatar and bio
- Message deletion (self/all for admin)
- Chat deletion
- Account deletion with confirmation
- Search within chat
- RGB themes
- Health check endpoint `/health`
- Database migrations on startup

## Security-аудит R6 (OWASP Top 10)

Аудит по OWASP Top 10 (2021). Статусы: **OK** — так и было, **FIXED** — усилено в фазе R6,
**N/A** — неприменимо. Улики — строки в коде/шаблонах на момент коммита фазы.

| Категория | Статус | Улики / что сделано |
|---|---|---|
| **A01 Broken Access Control** (IDOR) | FIXED | Проверки владения в каждом `/api/*`: правка — только автор `edit_message` (`Only author can edit`), удаление — участник/админ (`Not authorized to delete this message`, `Only admins can delete messages for all`), вложения — только участник диалога или группы (`GET /api/attachment/{id}` и `/info`, `Not a group member` / `Forbidden`), `blocks`/`pins` — только свои (`WHERE blocker_id = ? AND blocked_id = ?`, `DELETE FROM pins WHERE user_id = ?`), админ-эндпоинты — 15 вызовов `require_admin()` (строка 2747). Selftest: `t`, `u`, `w`. |
| **A02 Cryptographic Failures** | OK | Секреты вне логов (R4: `grep -icE "session=\|secret_key=" data/logs/*.log` → 0); ключ сессии — `HttpOnly`; `SameSite=Lax` по умолчанию, `None; Secure` **только** через `SESSION_SAME_SITE=none SESSION_SECURE=1` (строка 447); пароли — PBKDF2-HMAC-SHA256, 100 000 итераций, соль на пользователя (`hash_password`). |
| **A03 Injection** | FIXED | SQL — только параметризованный: `grep -nE "execute\(\s*f\"\|execute\([^)]*\+"` по `main.py` и `scripts/*.py` даёт 2 DDL-строки с константами, теперь с whitelist-проверкой имени колонки (`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name)`, строки 854, 884). `scripts/*` не делают shell-вызовов с пользовательскими данными (`subprocess` — только в `selftest.py`, с фиксированными аргументами). |
| **A04 Insecure Design** | OK | Rate-limit логина (5 попыток / 10 мин → 429), лимиты размера вложений (`MAX_UPLOAD_BYTES`, `THEME_IMAGE_MAX_BYTES`), удаление брошенных `.part`, invite-only регистрация после первого пользователя. |
| **A05 Security Misconfiguration** | FIXED | Заголовки на каждом ответе: `X-Content-Type-Options: nosniff` (стр. 666), `Referrer-Policy: no-referrer` (672), `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=(), usb=(), …` (674), CSP (678); `FastAPI(debug=(APP_MODE == "dev"))` (449) — трассировки клиенту только в dev; `/api/admin/debug-boom` в `prod` отдаёт 404. |
| **A06 Vulnerable Components** | OK | Все зависимости запинены: `requirements.txt` — 19 пакетов, все с `==` (fastapi 0.109.2, starlette 0.36.3, uvicorn 0.27.0, Jinja2 3.1.3 и т.д.). |
| **A07 Identification & Auth Failures** | FIXED | Смена пароля требует текущий (`Current password is incorrect`); логин пересоздаёт сессию — `request.session.clear()` перед записью (стр. 1465) — защита от session fixation; смена пароля поднимает `users.session_epoch` (3239) и рвёт сессии на других устройствах: `get_current_user` сверяет `session["epoch"]` с БД (1277), `смена пароля: user_id=…, прочие сессии сброшены (epoch=N)` в app.log. |
| **A08 Software & Data Integrity** | OK | `update.sh` с PRE/POST-чеком и автооткатом (R3), online-бэкап `backup.py` (R2), целостность БД `integrity_check ok` при старте, WAL. |
| **A09 Logging & Monitoring Failures** | OK | `data/logs/app.log` + `error.log` с ротацией, пульс-метрики, админ-аудит, необработанные исключения → `error.log` с traceback (R4). |
| **A10 SSRF** | N/A | Приложение не делает исходящих HTTP-запросов по пользовательским URL: внешних `fetch`/`requests` в коде нет. |

### CSRF (Phase R6)

При `SESSION_SAME_SITE=none` (работа в iframe) `SameSite` перестаёт защищать от CSRF — поэтому
включён double-submit:

- сервер выдаёт cookie `csrftoken` (`Path=/`, `SameSite`/`Secure` — как у сессии, **не** HttpOnly:
  фронтенд читает своё же значение) — `CSRFMiddleware`, `main.py:554`;
- на каждом **изменяющем** запросе (POST/PUT/PATCH/DELETE) значение должно прийти
  в заголовке `X-CSRF-Token` либо в поле формы `csrf_token` (HTML-формы и multipart-аплоады);
- сравнение — `hmac.compare_digest` (стр. 607), без токена или при несовпадении → **403**
  `{"detail": "CSRF token missing or invalid"}` + запись в лог `CSRF отклонён: …`;
- мидлварь — чистый ASGI (не `BaseHTTPMiddleware`): тело буферизуется и отдаётся приложению
  целиком, иначе эндпоинты получали бы пустой form; тела больше `MAX_UPLOAD_BYTES + 1 МБ` → 413;
- фронтенд: один патч `window.fetch` в `templates/chat.html` и `templates/admin.html`
  добавляет заголовок ко всем изменяющим запросам; HTML-формы `/login` и `/register`
  несут `{{ request.state.csrf_token }}` в скрытом поле.

### XSS-аудит (Phase R6)

Пользовательский текст попадает в DOM только через `escapeHtml`:

- `templates/chat.html` — пузырь сообщения собирается из экранированных частей:
  `${escapeHtml(msg.text)}`, `${escapeHtml(msg.sender_name)}`, цитата
  `${escapeHtml(msg.reply_to_name)}` / `${escapeHtml(snippet)}`, вложения
  `alt="${escapeHtml(att.orig_name)}"`, списки — `${escapeHtml(g.name)}`, `${escapeHtml(m.name)}`;
- `templates/admin.html` — добавлен `escapeHtml` и применён ко всем вставкам (инвайты
  `used_by_username`, аудит `actor`/`action`/`target`/`detail`, пульс `ts`/`kind`/`detail`);
- Jinja-шаблоны рендерятся с автоэкранированием, `| safe` в проекте не используется;
- CSP запрещает чужие скрипты: `script-src 'self' 'unsafe-inline'`.

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

## Конфигурация: .env, режимы, стартовые проверки

Приложение стартует одной командой `python main.py` (или `uvicorn main:app`): настройки
берутся из `.env` в корне репозитория — его читает `scripts/load_env.py` (stdlib, без зависимостей).

```bash
cp .env.example .env && nano .env
python main.py
```

Правила загрузки:
- формат `KEY=VALUE`, `#` — комментарий (строчный и хвостовой), значения можно в кавычках;
- **уже заданные переменные окружения не перезаписываются**: `SECRET_KEY=… python main.py` важнее файла;
- нет `.env` — не ошибка, работают значения по умолчанию.

Режимы (`APP_MODE`, по умолчанию `dev`):
- `dev` — слабый или отсутствующий `SECRET_KEY` разрешён, в `app.log` уходит warning;
- `prod` — `SECRET_KEY` обязан быть не короче 32 символов (и не `localdev`), иначе error в лог и **exit 1**.

Стартовые проверки (провал — exit 1 с понятным сообщением):
- `DATA_DIR` существует/создаётся и доступен для записи (пробный файл создаётся и удаляется);
- в `prod` — проверка `SECRET_KEY`.

Что получилось в итоге, видно в логе старта:
```
dev-режим: SECRET_KEY задан (64 символов)
сессия: cookie same_site=none secure=True; X-Frame-Options=none
старт приложения: DATA_DIR=./data, PORT=8000
journal_mode = wal
integrity_check ok
```

## Режим сессионной cookie и встраивание

По умолчанию cookie сессии — `SameSite=Lax` без `Secure`, а ответы содержат `X-Frame-Options: DENY`
(приложение открывается напрямую, не во фрейме). Если сервис открывают внутри iframe на другом
домене (например, предпросмотр в браузере), браузер такую cookie не отправит — после логина
`/chat` будет снова кидать на `/`. Для этого случая есть env-переключатели:

```bash
SESSION_SAME_SITE=none SESSION_SECURE=1 FRAME_OPTIONS=none python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- `SESSION_SAME_SITE` — `lax` (default) | `strict` | `none`;
- `SESSION_SECURE=1` — добавляет флаг `Secure` (обязателен для `SameSite=None`, нужен HTTPS);
- `FRAME_OPTIONS` — `deny` (default) | `sameorigin` | `none` (заголовок не отправляется).

Режим виден в логе при старте: `сессия: cookie same_site=... secure=...; X-Frame-Options=...`.

## УСТАВ (правила приёмки фаз)

1. Каждая фаза живёт в своей ветке; `main` не трогаем до явного решения о мерже.
2. Каждая фаза заканчивается коммитом с маркером `- phase <N> complete` (хотфиксы — `- <N>-fix complete`).
3. Маркер фазы дублируется в конец этого README — история фаз читается снизу вверх.
4. Меняем только то, что нужно фазе: никаких попутных переездов, переименований и «улучшений мимоходом».
5. `.gitignore` не редактируется без отдельной задачи.
6. Конфигурация — только через env (`SECRET_KEY`, `PORT`, `DATA_DIR`, `MAX_UPLOAD_BYTES`, `FIRST_USER_ADMIN`); значения по умолчанию в коде не подменяются.
7. `DATA_DIR` — единственный источник истины для персистентного состояния: БД и вложения выводятся из него, тесты указывают свой временный каталог.
8. Поведение по умолчанию не меняется: всё новое либо за флагом, либо обратно совместимо.
9. Логика прав (`require_admin`, матрица кика, доступ к вложениям) проверяется на сервере, а не в клиенте.
10. Фаза без воспроизводимой проверки считается незакрытой: скрипт, отчёт, маркер.
11. Фаза принимается **ТОЛЬКО** с зелёным `scripts/selftest.py`; последние 20 строк его вывода вкладываются в отчёт о фазе.
    Все соединения с SQLite создаются только через `get_conn()`; вложения пишутся в `*.part` и появляются через `os.replace`;
    `data/` целиком в `.gitignore`, поэтому `-wal`, `-shm` и `*.part` в git не попадают.
12. События приложения идут через `logging`, а не `print`: `data/logs/app.log` (INFO, ротация 5 МБ × 5) и
    `data/logs/error.log` (только ERROR+, своя ротация), формат `время | level | модуль | сообщение`.
    В логи не попадают cookie, `SECRET_KEY` и пароли — проверяется grep'ом по лог-вызовам:
    `grep -nE "(app_logger|error_logger|admin_logger|_login_logger|_ws_logger|_upload_logger)\.(info|warning|error|exception)" main.py | grep -iE "cookie|secret|password|token"`.

```bash
python scripts/selftest.py   # exit 0 только если все сценарии OK
```

Селftest поднимает отдельный инстанс на `PORT=8099` во временном `DATA_DIR`, удаляет его после прогона
и не трогает рабочий сервер; прогон занимает меньше 30 секунд.

## Installation on Raspberry Pi

### 1. Create system user and directories

```bash
sudo useradd -r -s /bin/false messenger
sudo mkdir -p /opt/messenger
sudo mkdir -p /var/lib/messenger/data
sudo mkdir -p /var/lib/messenger/backups
sudo chown -R messenger:messenger /var/lib/messenger
```

### 2. Clone and setup

```bash
cd /opt/messenger
sudo git clone <repository-url> .
sudo chown -R messenger:messenger /opt/messenger
```

### 3. Create virtual environment

```bash
cd /opt/messenger
sudo -u messenger python3 -m venv venv
sudo -u messenger ./venv/bin/pip install --upgrade pip
sudo -u messenger ./venv/bin/pip install -r requirements.txt
```

### 4. Configure environment

```bash
sudo mkdir -p /etc/messenger
sudo cp .env.example /etc/messenger/env
sudo nano /etc/messenger/env
```

Edit `/etc/messenger/env` (или просто `cp .env.example .env` в корне репозитория —
приложение само прочитает его при старте, см. ниже):
```
APP_MODE=prod
SECRET_KEY=<generate-with-python-c-secrets.token_hex(32)>
FIRST_USER_ADMIN=1   # Set to 1 ONLY for initial setup, then 0
MAX_UPLOAD_BYTES=5242880
THEME_IMAGE_MAX_BYTES=5242880
PORT=8000
DATA_DIR=/var/lib/messenger/data
BACKUP_DIR=/var/lib/messenger/backups
RETAIN_COUNT=5
UPDATE_BRANCH=main
```

Generate SECRET_KEY:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Install systemd service

```bash
sudo cp deploy/messenger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable messenger
sudo systemctl start messenger
sudo systemctl status messenger
```

### 6. Open firewall (if needed)

```bash
sudo ufw allow 8000/tcp
```

## Updating

Обновление — только через `scripts/update.sh`: он сначала прогоняет selftest (на сломанном
состоянии обновляться запрещено), снимает бэкап БД, делает `git fetch` + `reset --hard origin/<ветка>`,
перезапускает сервис (systemd или dev-режим) и снова прогоняет `/health` + selftest.
Зелёный постинчек — `data/last_good_hash` обновляется; красный — автоматический откат:
код на предыдущий HEAD, БД из бэкапа, рестарт.

```bash
cd /opt/messenger
sudo DATA_DIR=/var/lib/messenger/data BACKUP_DIR=/var/lib/messenger/backups \
     UPDATE_BRANCH=main ./scripts/update.sh
sudo DATA_DIR=/var/lib/messenger/data ./scripts/update.sh --dry-run   # только план, без изменений
```

Exit-коды: `0` — UPDATE OK, `1` — выполнен откат, `2` — pre-check красный,
`3` — не снялся бэкап, `4` — не удался fetch/reset.

Ручной вариант (без проверок, на свой страх и риск):

```bash
cd /opt/messenger
sudo git pull
sudo systemctl restart messenger
```

## Backup

Бэкап снимается онлайн, без остановки сервиса: `scripts/backup.sh` — тонкий шим
над `scripts/backup.py`, который копирует БД через SQLite Online Backup API
(`sqlite3.Connection.backup()`, корректно работает с WAL) и обязательно проверяет
**копию** через `PRAGMA integrity_check`. Битую копию скрипт бэкапом не считает:
печатает `ERROR: копия ... битая` и завершается ненулевым кодом.

Проверить уже существующую копию (например, перед восстановлением):

```bash
python3 scripts/backup.py --verify /var/lib/messenger/backups/messenger-YYYYMMDD_HHMMSS.db
# exit 0 — копия целая; exit 3 — битая
```

### Manual backup

```bash
sudo -u messenger /opt/messenger/scripts/backup.sh
```

### Automated backup (cron)

Add to crontab (`sudo crontab -e`):
```
0 2 * * * DATA_DIR=/var/lib/messenger/data BACKUP_DIR=/var/lib/messenger/backups RETAIN_COUNT=5 /opt/messenger/scripts/backup.sh
```

### Restore from backup

1. Stop the service:
   ```bash
   sudo systemctl stop messenger
   ```

2. Copy backup database:
   ```bash
   cp /var/lib/messenger/backups/messenger-YYYYMMDD_HHMMSS.db /var/lib/messenger/data/messenger.db
   ```

3. Extract uploads:
   ```bash
   tar -xzf /var/lib/messenger/backups/uploads-YYYYMMDD_HHMMSS.tar.gz -C /var/lib/messenger/data/
   ```

4. Start the service:
   ```bash
   sudo systemctl start messenger
   ```

## External Access

### Option 1: Tor Onion Service (Censorship-resistant)

**Pros:**
- No port forwarding required
- Hidden IP address
- Built-in encryption
- Resistant to DDoS

**Cons:**
- Requires Tor Browser for clients
- Slower connection speeds
- Not suitable for large file transfers

**Setup:**
```bash
sudo apt install tor
sudo nano /etc/tor/torrc
```

Add:
```
HiddenServiceDir /var/lib/tor/messenger/
HiddenServicePort 80 127.0.0.1:8000
```

Restart Tor:
```bash
sudo systemctl restart tor
cat /var/lib/tor/messenger/hostname
```

Share the `.onion` address with trusted users.

### Option 2: Port Forward + Self-Signed TLS

**Pros:**
- Direct HTTPS access from any browser
- Faster speeds
- Better for file transfers

**Cons:**
- Exposes server IP
- Requires port forwarding on router
- Self-signed certs trigger browser warnings
- Vulnerable to port scanning

**Setup:**
1. Forward port 8000 (or 443) on your router
2. Generate self-signed certificate:
   ```bash
   openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
     -keyout /etc/ssl/private/messenger.key \
     -out /etc/ssl/certs/messenger.crt
   ```
3. Use a reverse proxy (nginx) or configure uvicorn with SSL

## Configuration

Copy `.env.example` to `.env` and configure:

```
SECRET_KEY=your-secret-key-change-in-production
FIRST_USER_ADMIN=0
MAX_UPLOAD_BYTES=10485760
THEME_IMAGE_MAX_BYTES=5242880
PORT=8000
DATA_DIR=/var/lib/messenger/data
BACKUP_DIR=/var/lib/messenger/backups
```

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Secret key for session signing. **Must be set in production!** If not set, a fixed development key is used with a warning (sessions persist across restarts but this is insecure). |
| `FIRST_USER_ADMIN` | If `1`, first registered user becomes admin. Set to `0` after initial setup. |
| `MAX_UPLOAD_BYTES` | Maximum file upload size in bytes (default: 10MB) |
| `THEME_IMAGE_MAX_BYTES` | Maximum theme image (wallpaper/header/bubble) upload size in bytes (default: ~5MB) |
| `PORT` | Server port |
| `DATA_DIR` | Directory for database and uploads |
| `BACKUP_DIR` | Directory for backups |

## Running

```bash
export SECRET_KEY="your-secret-key"
export FIRST_USER_ADMIN=1
export PORT=8000
export DATA_DIR="./data"
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Or simply:
```bash
python main.py
```

## Testing

1. Open two browser windows (or use incognito)
2. Register first user → becomes admin (`is_admin=1`)
3. Register second user → regular user (`is_admin=0`)
4. Send messages between users - real-time via WebSocket
5. Test offline delivery: send message while recipient is offline, then log in as recipient
6. Test file uploads with Cyrillic filenames
7. Test admin features: warns, bans, unban
8. Test block functionality
9. Test profile, themes, search

## Database Schema

**users**: id, name, username (UNIQUE), password_hash, is_admin, created_at, banned_until, avatar_uuid, bio, theme_json
**messages**: id, sender_id, recipient_id, text, created_at, deleted_for_sender, deleted_for_recipient
**attachments**: id, message_id, uuid_name, orig_name, mime, size, created_at
**warns**: id, user_id, by_admin_id, reason, created_at
**invites**: code, created_by, used_by, created_at
**blocks**: blocker_id, blocked_id, created_at

## Health Check

```bash
curl http://localhost:8000/health
```

Returns: `{"status": "ok", "version": "5.0"}`

## License

Private use only.

- phase 1 complete

- phase 3.5 complete

- phase 4 complete

- phase 4.2 complete

- phase 4.3 complete

- phase 4.4 complete

- phase 4.4 publish marker

- phase 4.7 publish marker

- phase 4.8 final

- phase 5 complete

- phase 5.1 final

- phase 5.2c-1 complete

- phase 5.2d complete

- phase 5.2d2 complete

- phase 5.2d3 complete

- phase 5.2e complete

- phase 5.2g complete

- phase 5.2h complete

- phase 5.3 complete

- phase 6.6b complete

- phase 6.6c complete

- phase 7.1a complete

- hotfix 7.1a complete

- hotfix2 7.1a complete

- hotfix3 complete

- micro arrow complete

- phase 7.1b complete

- 7.1b-fix complete

- phase 7.1c complete

- phase 7.1d complete

- micro notify complete

- phase 7.2 complete

- phase 7.2b complete

- 7.2-fix complete

- micro toast complete

- phase 7.3 complete

- 7.3-fix complete

- 7.3-fix2 complete

- micro typing-width complete

- micro typing-toggle complete

- phase 7.4a complete

- 7.4a-fix complete

- 7.4a-fix2 complete
- 7.4a-fix3 complete
- 7.4a-fix4 complete
- phase 7.4b complete
- micro statusbar complete
- micro a11y complete
- micro a11y2 complete

- phase R1 complete

- phase R2 complete

- phase R3 complete

- phase R4 complete

- phase R5 complete

- phase R6 complete
