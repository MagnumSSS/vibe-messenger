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

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

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

Edit `/etc/messenger/env`:
```
SECRET_KEY=<generate-with-python-c-secrets.token_hex(32)>
FIRST_USER_ADMIN=1   # Set to 1 ONLY for initial setup, then 0
MAX_UPLOAD_BYTES=10485760
THEME_IMAGE_MAX_BYTES=5242880
PORT=8000
DATA_DIR=/var/lib/messenger/data
BACKUP_DIR=/var/lib/messenger/backups
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

```bash
cd /opt/messenger
sudo git pull
sudo systemctl restart messenger
```

## Backup

### Manual backup

```bash
sudo -u messenger /opt/messenger/scripts/backup.sh
```

### Automated backup (cron)

Add to crontab (`sudo crontab -e`):
```
0 2 * * * DATA_DIR=/var/lib/messenger/data BACKUP_DIR=/var/lib/messenger/backups RETAIN_COUNT=7 /opt/messenger/scripts/backup.sh
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
