# Private Messenger - Phase 1

A lightweight private web messenger for small groups, designed for Raspberry Pi 3B+.

## Features

- User registration and login with session-based authentication
- First user gets admin role (configurable via `FIRST_USER_ADMIN`)
- One-on-one text messaging
- Real-time message delivery via WebSocket
- Offline message storage in SQLite
- Responsive design for mobile and desktop

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and configure:

```
SECRET_KEY=your-secret-key-change-in-production
FIRST_USER_ADMIN=1
MAX_UPLOAD_BYTES=1048576
PORT=8000
DATA_DIR=./data
```

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Secret key for session signing. **Must be set in production!** If not set, a fixed development key is used with a warning (sessions persist across restarts but this is insecure). |
| `FIRST_USER_ADMIN` | If `1`, first registered user becomes admin |
| `MAX_UPLOAD_BYTES` | Maximum message length in bytes |
| `PORT` | Server port |
| `DATA_DIR` | Directory for database and uploads |

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

## Database Schema

**users**: id, name, username (UNIQUE), password_hash, is_admin, created_at
**messages**: id, sender_id, recipient_id, text, created_at

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
