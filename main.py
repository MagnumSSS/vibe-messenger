import os
import sqlite3
import hashlib
import secrets
import uuid
import mimetypes
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote


import aiofiles
import aiofiles.os
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles 

# Config from env
SECRET_KEY = os.environ.get("SECRET_KEY")
if SECRET_KEY is None:
    # Fixed dev key for development - DO NOT use in production
    SECRET_KEY = "dev-secret-key-change-in-production"
    import sys
    print("=" * 80, file=sys.stderr)
    print("WARNING: SECRET_KEY not set! Using fixed development key.", file=sys.stderr)
    print("All sessions will persist across restarts, but this is INSECURE for production.", file=sys.stderr)
    print("Please set the SECRET_KEY environment variable in production.", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
FIRST_USER_ADMIN = os.environ.get("FIRST_USER_ADMIN", "1") == "1"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "10485760"))  # 10MB default
THEME_IMAGE_MAX_BYTES = int(os.environ.get("THEME_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))  # ~5MB for theme image slots (separate from MAX_UPLOAD_BYTES)
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = os.environ.get("DATA_DIR", "./data")

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "messenger.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
THEME_IMAGES_DIR = os.path.join(DATA_DIR, "theme_images")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(THEME_IMAGES_DIR, exist_ok=True)

# ========== THEME TOKENS MANIFEST (Phase 5.2) ==========
# Data-driven theme engine: each token has key, css_var, default, type
THEME_TOKENS = {
    "colors": {
        "bg": {"key": "bg", "css_var": "--bg-color", "default": "#f0f2f5", "type": "color"},
        "panel": {"key": "panel", "css_var": "--panel-color", "default": "#ffffff", "type": "color"},
        "header": {"key": "header", "css_var": "--header-color", "default": "#0084ff", "type": "color"},
        "text": {"key": "text", "css_var": "--text-primary", "default": "#333333", "type": "color"},
        "muted": {"key": "muted", "css_var": "--text-secondary", "default": "#666666", "type": "color"},
        "accent": {"key": "accent", "css_var": "--accent-color", "default": "#0084ff", "type": "color"},
        "btn_primary": {"key": "btn_primary", "css_var": "--btn-primary-bg", "default": "#0084ff", "type": "color"},
        "btn_danger": {"key": "btn_danger", "css_var": "--btn-danger-bg", "default": "#dc3545", "type": "color"},
        "bubble_sent": {"key": "bubble_sent", "css_var": "--bubble-sent-bg", "default": "#0084ff", "type": "color"},
        "bubble_received": {"key": "bubble_received", "css_var": "--bubble-received-bg", "default": "#e4e6eb", "type": "color"},
        "input_bg": {"key": "input_bg", "css_var": "--input-bg", "default": "#ffffff", "type": "color"},
        # "border" removed in Phase 5.2h: --border-color is auto-derived from bg (Phase 5.2d3)
        "modal_bg": {"key": "modal_bg", "css_var": "--modal-bg", "default": "#ffffff", "type": "color"},
        "hover": {"key": "hover", "css_var": "--hover-bg", "default": "#f5f5f5", "type": "color"},
        "active": {"key": "active", "css_var": "--active-bg", "default": "#e6f2ff", "type": "color"},
    },
    "images": {
        "header_img": {"key": "header_img", "css_var": "--header-img", "default": None, "type": "image"},
        "wallpaper": {"key": "wallpaper", "css_var": "--wallpaper-img", "default": None, "type": "image"},
        "bubble_img": {"key": "bubble_img", "css_var": "--bubble-img", "default": None, "type": "image"},
    },
    # Phase 5.4: blur effect tokens - range sliders in the editor (0..20 px)
    "effects": {
        "wallpaper_blur": {"key": "wallpaper_blur", "css_var": "--wallpaper-blur", "default": 0, "type": "range", "min": 0, "max": 20, "unit": "px"},
        "bubble_blur": {"key": "bubble_blur", "css_var": "--bubble-blur", "default": 0, "type": "range", "min": 0, "max": 20, "unit": "px"},
    }
}

# Presets for quick theme switching
THEME_PRESETS = {
    "default": {
        "colors": {
            "bg": "#f0f2f5",
            "panel": "#ffffff",
            "header": "#0084ff",
            "text": "#333333",
            "muted": "#666666",
            "accent": "#0084ff",
            "btn_primary": "#0084ff",
            "btn_danger": "#dc3545",
            "bubble_sent": "#0084ff",
            "bubble_received": "#e4e6eb",
            "input_bg": "#ffffff",
            "border": "#dddddd",
            "modal_bg": "#ffffff",
            "hover": "#f5f5f5",
            "active": "#e6f2ff",
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0}
    },
    "dark": {
        "colors": {
            "bg": "#1a1a2e",
            "panel": "#16213e",
            "header": "#0f3460",
            "text": "#eaeaea",
            "muted": "#a0a0a0",
            "accent": "#e94560",
            "btn_primary": "#e94560",
            "btn_danger": "#c0392b",
            "bubble_sent": "#0f3460",
            "bubble_received": "#2d3436",
            "input_bg": "#16213e",
            "border": "#333333",
            "modal_bg": "#16213e",
            "hover": "#22304f",
            "active": "#2a3a5f",
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0}
    }
}


def merge_theme_with_defaults(theme_json_str):
    """Merge user theme with defaults, handling v1->v2 migration"""
    import json
    try:
        if not theme_json_str:
            return THEME_PRESETS["default"]
        theme = json.loads(theme_json_str)
        # Handle v1 format (flat dict) -> v2 format (colors/images)
        if isinstance(theme, dict) and "colors" not in theme:
            # v1 format: {"primary": "#...", "sent": "#...", ...}
            # Migrate to v2
            v2_theme = {"colors": {}, "images": {}}
            color_mapping = {
                "primary": "accent",
                "sent": "bubble_sent",
                "received": "bubble_received",
                "bg": "bg",
            }
            for old_key, new_key in color_mapping.items():
                if old_key in theme:
                    v2_theme["colors"][new_key] = theme[old_key]
            theme = v2_theme
        # Merge with defaults
        result = {
            "colors": {**THEME_PRESETS["default"]["colors"], **theme.get("colors", {})},
            "images": {**THEME_PRESETS["default"]["images"], **theme.get("images", {})},
            "effects": {**THEME_PRESETS["default"].get("effects", {}), **theme.get("effects", {})}
        }
        return result
    except Exception as e:
        print(f"Theme merge error: {e}")
        return THEME_PRESETS["default"]


def hex_to_rgb(hex_color):
    """Convert hex color to RGB tuple"""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6:
        return (51, 51, 51)  # Default dark gray
    try:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except ValueError:
        return (51, 51, 51)


def calculate_yiq_contrast(hex_color):
    """Calculate YIQ brightness and return appropriate text color (dark or light)"""
    r, g, b = hex_to_rgb(hex_color)
    yiq = (r * 299 + g * 587 + b * 114) / 1000
    return "#000000" if yiq >= 128 else "#ffffff"


app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

templates = Jinja2Templates(directory="templates")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()




def ensure_schema():
    """
    Ensure database schema is up-to-date by adding missing tables and columns.
    Called on every startup to handle migrations from older versions.
    Uses a declarative approach: list of (table, column, type) tuples.
    """
    with get_db() as conn:
        # Create users table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Get current columns in users table
        user_columns = {col[1] for col in conn.execute("PRAGMA table_info(users)").fetchall()}
        
        # Add missing columns to users table (Phase 3-4 additions)
        user_column_additions = [
            ("banned_until", "TIMESTAMP NULL"),      # Phase 3: ban functionality
            ("avatar_uuid", "TEXT NULL"),            # Phase 4: profile avatar
            ("bio", "TEXT NULL"),                    # Phase 4: profile bio
            ("theme_json", "TEXT NULL"),             # Phase 4: RGB themes
        ]
        for col_name, col_type in user_column_additions:
            if col_name not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                print(f"Added column users.{col_name}")
        
        # Create messages table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(id),
                FOREIGN KEY (recipient_id) REFERENCES users(id)
            )
        """)
        
        # Get current columns in messages table
        msg_columns = {col[1] for col in conn.execute("PRAGMA table_info(messages)").fetchall()}
        
        # Add missing columns to messages table (Phase 4: soft delete)
        msg_column_additions = [
            ("deleted_for_sender", "INTEGER DEFAULT 0"),
            ("deleted_for_recipient", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in msg_column_additions:
            if col_name not in msg_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
                print(f"Added column messages.{col_name}")
        
        # Create attachments table if not exists (Phase 2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                uuid_name TEXT NOT NULL,
                orig_name TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        """)
        
        # Create warns table if not exists (Phase 3)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                by_admin_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (by_admin_id) REFERENCES users(id)
            )
        """)
        
        # Create invites table if not exists (Phase 3)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                created_by INTEGER NOT NULL,
                used_by INTEGER NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(id),
                FOREIGN KEY (used_by) REFERENCES users(id)
            )
        """)
        
        # Create blocks table if not exists (Phase 3)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (blocker_id, blocked_id),
                FOREIGN KEY (blocker_id) REFERENCES users(id),
                FOREIGN KEY (blocked_id) REFERENCES users(id)
            )
        """)
        
        conn.commit()


ensure_schema()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"{salt}:{pwd_hash}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, stored_hash = password_hash.split(":")
    except ValueError:
        return False
    new_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return secrets.compare_digest(new_hash, stored_hash)


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_current_user_fresh(request: Request):
    """Get current user with fresh data from DB (for ban check)"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring and load balancers."""
    return JSONResponse({"status": "ok", "version": "5.0"})


@app.get("/")
async def index(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.get("/register")
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse(request, "register.html", {"error": None, "invite_code": request.query_params.get("invite", "")})


@app.post("/register")
async def register(request: Request, name: str = Form(...), username: str = Form(...), password: str = Form(...), invite_code: str = Form("")):
    import re
    
    # Strip @ prefix if user entered it
    username_clean = username.lstrip('@')
    
    # Validate username format: only latin letters, digits, underscore
    if not re.match(r'^[a-zA-Z0-9_]+$', username_clean):
        return templates.TemplateResponse(request, "register.html", {
            "error": "Username must contain only Latin letters, digits, and underscore",
            "invite_code": invite_code,
            "name": name,
            "username": username_clean
        })
    
    if len(username_clean) < 3:
        return templates.TemplateResponse(request, "register.html", {
            "error": "Username must be at least 3 characters",
            "invite_code": invite_code,
            "name": name,
            "username": username_clean
        })
    
    if len(password) < 4:
        return templates.TemplateResponse(request, "register.html", {
            "error": "Password must be at least 4 characters",
            "invite_code": invite_code,
            "name": name,
            "username": username_clean
        })
    
    password_hash = hash_password(password)
    
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username_clean,)).fetchone()
        if existing:
            return templates.TemplateResponse(request, "register.html", {
                "error": "Username already exists",
                "invite_code": invite_code,
                "name": name,
                "username": username_clean
            })
        
        # Check if this is the first user
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = 1 if (count == 0 and FIRST_USER_ADMIN) else 0
        
        # Validate invite code (not required for first user)
        if count > 0 or not FIRST_USER_ADMIN:
            if not invite_code:
                return templates.TemplateResponse(request, "register.html", {
                    "error": "Invite code is required",
                    "invite_code": invite_code,
                    "name": name,
                    "username": username_clean
                })
            
            invite = conn.execute("SELECT * FROM invites WHERE code = ? AND used_by IS NULL", (invite_code,)).fetchone()
            if not invite:
                return templates.TemplateResponse(request, "register.html", {
                    "error": "Invalid or expired invite code",
                    "invite_code": invite_code,
                    "name": name,
                    "username": username_clean
                })
        
        conn.execute(
            "INSERT INTO users (name, username, password_hash, is_admin) VALUES (?, ?, ?, ?)",
            (name, username_clean, password_hash, is_admin)
        )
        new_user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Mark invite as used
        if invite_code and count > 0:
            conn.execute("UPDATE invites SET used_by = ? WHERE code = ?", (new_user_id, invite_code))
        
        conn.commit()
    
    return RedirectResponse(url="/", status_code=303)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})
    
    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/chat", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat")
async def chat_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    
    with get_db() as conn:
        users = conn.execute("SELECT id, name, username, avatar_uuid FROM users WHERE id != ?", (user["id"],)).fetchall()
    
    # Ensure theme_json is never None - default to '{}'
    if user.get("theme_json") is None:
        user["theme_json"] = "{}"
    
    return templates.TemplateResponse(request, "chat.html", {
        "user": user,
        "users": [dict(u) for u in users],
        "max_upload_bytes": MAX_UPLOAD_BYTES
    })


@app.get("/api/users")
async def api_users(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        users = conn.execute("SELECT id, name, username, avatar_uuid, bio FROM users WHERE id != ?", (user["id"],)).fetchall()
    
    return JSONResponse([dict(u) for u in users])


@app.get("/api/user/{user_id}/profile")
async def api_user_profile(request: Request, user_id: int):
    """Get another user's profile (name, username, bio, avatar_uuid) - requires auth"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        row = conn.execute("SELECT id, name, username, avatar_uuid, bio FROM users WHERE id = ?", (user_id,)).fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    
    return JSONResponse(dict(row))


@app.get("/api/messages/{recipient_id}")
async def api_messages(request: Request, recipient_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        messages = conn.execute("""
            SELECT m.id, m.sender_id, m.recipient_id, m.text, m.created_at, 
                   sender.avatar_uuid as sender_avatar_uuid
            FROM messages m
            JOIN users sender ON m.sender_id = sender.id
            WHERE (m.sender_id = ? AND m.recipient_id = ?) OR (m.sender_id = ? AND m.recipient_id = ?)
            ORDER BY m.created_at ASC
        """, (user["id"], recipient_id, recipient_id, user["id"])).fetchall()
        
        # Build response with attachments for each message
        result = []
        for msg in messages:
            msg_dict = dict(msg)
            attachments = conn.execute(
                "SELECT id, uuid_name, orig_name, mime, size, created_at FROM attachments WHERE message_id = ?",
                (msg_dict["id"],)
            ).fetchall()
            msg_dict["attachments"] = [dict(a) for a in attachments]
            result.append(msg_dict)
    
    return JSONResponse(result)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    try:
        await websocket.accept()
    except Exception:
        # Connection closed before handshake completed
        return
    
    # Get user from session cookie
    cookies = websocket.cookies
    session_data = cookies.get("session")
    
    if not session_data:
        try:
            await websocket.close(code=4001)
        except Exception:
            pass  # Already closed
        return
    
    # Parse session to get user_id (simplified - in production use itsdangerous)
    from itsdangerous import URLSafeTimedSerializer, BadSignature
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    try:
        session = serializer.loads(session_data)
        user_id = session.get("user_id")
    except BadSignature:
        try:
            await websocket.close(code=4001)
        except Exception:
            pass
        return
    
    if not user_id:
        try:
            await websocket.close(code=4001)
        except Exception:
            pass
        return
    
    # Store connection
    app.state.connections[user_id] = websocket
    
    try:
        while True:
            data = await websocket.receive_json()
            # Handle incoming messages if needed
    except WebSocketDisconnect:
        if user_id in app.state.connections:
            del app.state.connections[user_id]
    except Exception:
        if user_id in app.state.connections:
            del app.state.connections[user_id]


# Store active connections
app.state.connections = {}


def is_participant(user_id, recipient_id):
    """Check if user is participant in a dialog with recipient_id"""
    with get_db() as conn:
        # Check if there are any messages between these users
        msg = conn.execute("""
            SELECT 1 FROM messages 
            WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
            LIMIT 1
        """, (user_id, recipient_id, recipient_id, user_id)).fetchone()
        return msg is not None or user_id == recipient_id


async def stream_file(file_path: str):
    """Stream file in chunks using aiofiles"""
    async with aiofiles.open(file_path, 'rb') as f:
        while True:
            chunk = await f.read(65536)  # 64KB chunks
            if not chunk:
                break
            yield chunk


@app.get("/api/theme/image/{image_type}")
async def get_theme_image(request: Request, image_type: str):
    """Serve theme image (header_img, wallpaper, bubble_img) - requires auth"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    valid_types = set(THEME_TOKENS.get("images", {}).keys())
    if image_type not in valid_types:
        raise HTTPException(status_code=404, detail="Image type not found")
    
    # Get user's theme to find the image UUID
    with get_db() as conn:
        row = conn.execute("SELECT theme_json FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    
    theme_data = merge_theme_with_defaults(row["theme_json"] if row else None)
    image_uuid = theme_data.get("images", {}).get(image_type)
    
    if not image_uuid:
        raise HTTPException(status_code=404, detail="Image not set")
    
    file_path = os.path.join(THEME_IMAGES_DIR, image_uuid)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image file not found")
    
    # Guess mime type
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    
    return StreamingResponse(
        stream_file(file_path),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{image_uuid}"',
            # Phase 5.3 bugfix: replacing a slot image must never serve a stale cached copy
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/theme/image/{image_type}")
async def upload_theme_image(request: Request, image_type: str, image: UploadFile = File(...)):
    """Upload theme image (header_img, wallpaper, bubble_img) - requires auth"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    valid_types = set(THEME_TOKENS.get("images", {}).keys())
    if image_type not in valid_types:
        raise HTTPException(status_code=400, detail="Invalid image type")

    # Validate file
    if not image.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    mime_type = image.content_type or mimetypes.guess_type(image.filename)[0]
    if not mime_type or not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    # Generate UUID filename (keep extension so mimetypes can sniff on serve)
    uuid_name = f"{uuid.uuid4().hex}{Path(image.filename).suffix.lower()}"
    file_path = os.path.join(THEME_IMAGES_DIR, uuid_name)

    # Save file in chunks with size limit
    total_bytes = 0
    async with aiofiles.open(file_path, 'wb') as out_file:
        while True:
            chunk = await image.read(65536)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > THEME_IMAGE_MAX_BYTES:
                await out_file.close()
                await aiofiles.os.remove(file_path)
                raise HTTPException(
                    status_code=400,
                    detail=f"Image exceeds size limit ({THEME_IMAGE_MAX_BYTES // 1024} KB)"
                )
            await out_file.write(chunk)

    # Update user's theme_json, replacing any previous image of this type
    with get_db() as conn:
        row = conn.execute("SELECT theme_json FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        theme_data = merge_theme_with_defaults(row["theme_json"] if row else None)
        old_uuid = theme_data.get("images", {}).get(image_type)
        theme_data["images"][image_type] = uuid_name
        conn.execute("UPDATE users SET theme_json = ? WHERE id = ?", (
            __import__('json').dumps(theme_data), current_user["id"]
        ))
        conn.commit()

    if old_uuid and old_uuid != uuid_name:
        old_path = os.path.join(THEME_IMAGES_DIR, old_uuid)
        if os.path.exists(old_path):
            try:
                await aiofiles.os.remove(old_path)
            except Exception:
                pass

    return JSONResponse({"success": True, "uuid": uuid_name})


@app.delete("/api/theme/image/{image_type}")
async def delete_theme_image(request: Request, image_type: str):
    """Remove theme image - requires auth"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    valid_types = set(THEME_TOKENS.get("images", {}).keys())
    if image_type not in valid_types:
        raise HTTPException(status_code=400, detail="Invalid image type")

    with get_db() as conn:
        row = conn.execute("SELECT theme_json FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        theme_data = merge_theme_with_defaults(row["theme_json"] if row else None)

        # Remove the image reference
        old_uuid = theme_data.get("images", {}).get(image_type)
        theme_data["images"][image_type] = None
        
        conn.execute("UPDATE users SET theme_json = ? WHERE id = ?", (
            __import__('json').dumps(theme_data), current_user["id"]
        ))
        conn.commit()
    
    # Delete the actual file if exists
    if old_uuid:
        old_path = os.path.join(THEME_IMAGES_DIR, old_uuid)
        if os.path.exists(old_path):
            try:
                await aiofiles.os.remove(old_path)
            except Exception:
                pass
    
    return JSONResponse({"success": True})


@app.post("/api/send")
async def send_message(
    request: Request,
    recipient_id: int = Form(...),
    text: str = Form(""),
    files: list[UploadFile] = File(default=[])
):
    user = get_current_user_fresh(request)  # Use fresh data to catch ban status
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Check if user is banned
    if is_banned(user):
        banned_until = datetime.fromisoformat(user["banned_until"].replace("Z", "+00:00").replace("+00:00", ""))
        raise HTTPException(status_code=403, detail=f"You are banned until {banned_until.strftime('%Y-%m-%d %H:%M')}")
    
    # Check if there's a block between users
    if check_block(user["id"], recipient_id):
        raise HTTPException(status_code=403, detail="Communication is blocked")
    
    # Allow empty text only if there are attachments
    if not text.strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message")
    
    if len(text) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Message too long")
    
    with get_db() as conn:
        # Verify recipient exists
        recipient = conn.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient not found")

        # FIX BAN: Check if recipient is banned - cannot send to banned user
        recipient_row = conn.execute("SELECT banned_until FROM users WHERE id = ?", (recipient_id,)).fetchone()
        if recipient_row and recipient_row[0]:
            try:
                banned_until = datetime.fromisoformat(recipient_row[0].replace("Z", "+00:00").replace("+00:00", ""))
                if banned_until > datetime.now():
                    raise HTTPException(status_code=403, detail="Пользователь недоступен")
            except (ValueError, AttributeError):
                pass

        
        conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, text) VALUES (?, ?, ?)",
            (user["id"], recipient_id, text)
        )
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Handle file uploads with chunked streaming (64KB chunks)
        attachment_ids = []
        for file in files:
            if file.filename:
                # Generate UUID filename with extension
                ext = Path(file.filename).suffix.lower()
                uuid_name = f"{uuid.uuid4().hex}{ext}"
                file_path = os.path.join(UPLOADS_DIR, uuid_name)
                
                # Stream file to disk in 64KB chunks while counting bytes
                total_bytes = 0
                chunk_size = 64 * 1024  # 64KB
                async with aiofiles.open(file_path, 'wb') as f:
                    while True:
                        # Phase 5.3 fix: UploadFile.read is the async API; .file.read is sync and cannot be awaited
                        chunk = await file.read(chunk_size)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > MAX_UPLOAD_BYTES:
                            # Delete partial file on exceed
                            await aiofiles.os.remove(file_path)
                            raise HTTPException(status_code=400, detail=f"File '{file.filename}' exceeds size limit")
                        await f.write(chunk)
                
                # Detect mime type from extension
                mime_type = mimetypes.guess_type(file.filename)[0] or file.content_type or "application/octet-stream"
                
                # Store attachment in DB
                conn.execute(
                    "INSERT INTO attachments (message_id, uuid_name, orig_name, mime, size) VALUES (?, ?, ?, ?, ?)",
                    (message_id, uuid_name, file.filename, mime_type, total_bytes)
                )
                attachment_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        
        conn.commit()
        
        # Get the inserted message with attachments
        msg = conn.execute(
            "SELECT id, sender_id, recipient_id, text, created_at FROM messages WHERE id = ?",
            (message_id,)
        ).fetchone()
        
        # Get attachments for this message
        attachments = conn.execute(
            "SELECT id, uuid_name, orig_name, mime, size, created_at FROM attachments WHERE message_id = ?",
            (message_id,)
        ).fetchall()
    
    # Build response with attachments
    msg_dict = dict(msg)
    msg_dict["attachments"] = [dict(a) for a in attachments]
    
    # Send via WebSocket if recipient is online
    if recipient_id in app.state.connections:
        try:
            await app.state.connections[recipient_id].send_json(msg_dict)
        except:
            pass  # Connection might have closed
    
    return JSONResponse(msg_dict)


@app.get("/api/attachment/{attachment_id}")
async def get_attachment(request: Request, attachment_id: int):
    """Serve attachment file - requires auth and dialog participation"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        # Get attachment info
        att = conn.execute("""
            SELECT a.id, a.uuid_name, a.orig_name, a.mime, a.size, m.sender_id, m.recipient_id
            FROM attachments a
            JOIN messages m ON a.message_id = m.id
            WHERE a.id = ?
        """, (attachment_id,)).fetchone()
        
        if not att:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        # Check if user is participant in the dialog
        if user["id"] != att["sender_id"] and user["id"] != att["recipient_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
    
    # Serve the file
    file_path = os.path.join(UPLOADS_DIR, att["uuid_name"])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    
    safe_ascii = att["orig_name"].encode("ascii", "ignore").decode("ascii") or "file"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(att['orig_name'], safe='')}"
    }

    return StreamingResponse(
        stream_file(file_path),
        media_type=att["mime"],
        headers=headers
    )


@app.get("/api/attachment/{attachment_id}/info")
async def get_attachment_info(request: Request, attachment_id: int):
    """Get attachment metadata - requires auth and dialog participation"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        att = conn.execute("""
            SELECT a.id, a.uuid_name, a.orig_name, a.mime, a.size, a.created_at, m.sender_id, m.recipient_id
            FROM attachments a
            JOIN messages m ON a.message_id = m.id
            WHERE a.id = ?
        """, (attachment_id,)).fetchone()
        
        if not att:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        # Check if user is participant in the dialog
        if user["id"] != att["sender_id"] and user["id"] != att["recipient_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
    
    return JSONResponse(dict(att))


# ============== Phase 3: Admin Panel, Invites, Bans, Blocks ==============

def require_admin(user):
    """Check if user is admin, raise 403 if not"""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


def is_banned(user):
    """Check if user is currently banned"""
    if not user.get("banned_until"):
        return False
    banned_until = datetime.fromisoformat(user["banned_until"].replace("Z", "+00:00").replace("+00:00", ""))
    if banned_until > datetime.now():
        return True
    return False


def check_block(sender_id, recipient_id):
    """Check if there's a block between two users (either direction)"""
    with get_db() as conn:
        block = conn.execute("""
            SELECT 1 FROM blocks 
            WHERE (blocker_id = ? AND blocked_id = ?) OR (blocker_id = ? AND blocked_id = ?)
        """, (sender_id, recipient_id, recipient_id, sender_id)).fetchone()
        return block is not None


@app.get("/admin")
async def admin_page(request: Request):
    """Admin panel - list all users"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        all_users = conn.execute("""
            SELECT id, name, username, is_admin, banned_until, created_at 
            FROM users 
            ORDER BY created_at DESC
        """).fetchall()
        
        # Get warn counts for each user
        users_with_warns = []
        for u in all_users:
            user_dict = dict(u)
            warn_count = conn.execute("SELECT COUNT(*) FROM warns WHERE user_id = ?", (user_dict["id"],)).fetchone()[0]
            user_dict["warn_count"] = warn_count
            user_dict["is_banned"] = is_banned(user_dict)
            users_with_warns.append(user_dict)
    
    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": users_with_warns
    })


@app.post("/admin/invite")
async def create_invite(request: Request):
    """Create a new invite code"""
    user = get_current_user(request)
    require_admin(user)
    
    # Generate random invite code
    invite_code = secrets.token_urlsafe(8)
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_by) VALUES (?, ?)",
            (invite_code, user["id"])
        )
        conn.commit()
    
    return JSONResponse({"invite_code": invite_code})


@app.get("/api/admin/invites")
async def get_invites(request: Request):
    """Get all invite codes"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        invites = conn.execute("""
            SELECT i.code, i.created_by, i.used_by, i.created_at, u.username as used_by_username
            FROM invites i
            LEFT JOIN users u ON i.used_by = u.id
            ORDER BY i.created_at DESC
        """).fetchall()
    
    return JSONResponse([dict(i) for i in invites])


@app.post("/admin/warn")
async def warn_user(request: Request, target_user_id: int = Form(...), reason: str = Form(...)):
    """Issue a warning to a user. 3 warnings = auto-ban"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        # Check target exists
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Add warning
        conn.execute(
            "INSERT INTO warns (user_id, by_admin_id, reason) VALUES (?, ?, ?)",
            (target_user_id, user["id"], reason)
        )
        
        # Count warnings
        warn_count = conn.execute("SELECT COUNT(*) FROM warns WHERE user_id = ?", (target_user_id,)).fetchone()[0]
        
        # Auto-ban if 3 warnings
        if warn_count >= 3:
            banned_until = datetime.now() + timedelta(days=7)  # Default 7 day ban
            conn.execute("UPDATE users SET banned_until = ? WHERE id = ?", (banned_until.isoformat(), target_user_id))
            conn.commit()
            return JSONResponse({"warn_count": warn_count, "auto_banned": True, "banned_until": banned_until.isoformat()})
        
        conn.commit()
    
    return JSONResponse({"warn_count": warn_count, "auto_banned": False})


@app.post("/admin/ban")
async def ban_user(request: Request, target_user_id: int = Form(...), days: int = Form(7)):
    """Ban a user for N days"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        banned_until = datetime.now() + timedelta(days=days)
        conn.execute("UPDATE users SET banned_until = ? WHERE id = ?", (banned_until.isoformat(), target_user_id))
        conn.commit()
    
    return JSONResponse({"banned_until": banned_until.isoformat()})


@app.post("/admin/unban")
async def unban_user(request: Request, target_user_id: int = Form(...)):
    """Unban a user"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        conn.execute("UPDATE users SET banned_until = NULL WHERE id = ?", (target_user_id,))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.post("/admin/grant-admin")
async def grant_admin(request: Request, target_user_id: int = Form(...)):
    """Grant admin privileges to a user"""
    user = get_current_user(request)
    require_admin(user)
    
    with get_db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (target_user_id,))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.post("/admin/revoke-admin")
async def revoke_admin(request: Request, target_user_id: int = Form(...)):
    """Revoke admin privileges from a user"""
    user = get_current_user(request)
    require_admin(user)
    
    # Don't allow revoking own admin
    if target_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot revoke your own admin privileges")
    
    with get_db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (target_user_id,))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.get("/api/settings/blocks")
async def get_blocks(request: Request):
    """Get list of users that current user has blocked"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        blocks = conn.execute("""
            SELECT u.id, u.username, u.name, b.created_at
            FROM blocks b
            JOIN users u ON b.blocked_id = u.id
            WHERE b.blocker_id = ?
            ORDER BY b.created_at DESC
        """, (user["id"],)).fetchall()
    
    return JSONResponse([dict(b) for b in blocks])


@app.post("/api/block")
async def block_user(request: Request, target_user_id: int = Form(...)):
    """Block another user"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if target_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    
    with get_db() as conn:
        target = conn.execute("SELECT id FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        conn.execute("""
            INSERT OR REPLACE INTO blocks (blocker_id, blocked_id, created_at) 
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (user["id"], target_user_id))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.post("/api/unblock")
async def unblock_user(request: Request, target_user_id: int = Form(...)):
    """Unblock a user"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], target_user_id))
        conn.commit()
    
    return JSONResponse({"success": True})

app.mount("/static", StaticFiles(directory="static"), name="static")



# ========== PROFILE ENDPOINTS ==========
@app.get("/api/profile")
async def get_profile(request: Request):
    """Get current user's profile data"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        row = conn.execute("SELECT id, name, username, avatar_uuid, bio FROM users WHERE id = ?", (user["id"],)).fetchone()
    
    return JSONResponse(dict(row))


@app.post("/api/profile")
async def update_profile(request: Request, name: str = Form(...), bio: str = Form("")):
    """Update current user's profile (name and bio)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("UPDATE users SET name = ?, bio = ? WHERE id = ?", (name, bio, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.put("/api/profile")
async def update_profile_put(request: Request, name: str = Form(...), bio: str = Form("")):
    """Update current user's profile (PUT method)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("UPDATE users SET name = ?, bio = ? WHERE id = ?", (name, bio, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.put("/api/theme")
async def save_theme_put(request: Request, theme_json: str = Form(...)):
    """Save theme settings for current user (PUT method)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("UPDATE users SET theme_json = ? WHERE id = ?", (theme_json, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True})


@app.post("/api/profile/avatar")
async def upload_avatar(request: Request, avatar: UploadFile = File(...)):
    """Upload avatar for current user"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate file
    if not avatar.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    # Check if image
    mime_type = avatar.content_type or mimetypes.guess_type(avatar.filename)[0]
    if not mime_type or not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")
    
    # Generate UUID filename
    ext = Path(avatar.filename).suffix.lower()
    uuid_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOADS_DIR, "avatars", uuid_name)
    
    # Ensure avatars directory exists
    os.makedirs(os.path.join(UPLOADS_DIR, "avatars"), exist_ok=True)
    
    # Save file
    file_data = await avatar.read()
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(file_data)
    
    # Update user record
    with get_db() as conn:
        conn.execute("UPDATE users SET avatar_uuid = ? WHERE id = ?", (uuid_name, user["id"]))
        conn.commit()
    
    return JSONResponse({"avatar_uuid": uuid_name})


@app.get("/api/avatar/{avatar_uuid}")
async def get_avatar(avatar_uuid: str):
    """Serve user avatar"""
    file_path = os.path.join(UPLOADS_DIR, "avatars", avatar_uuid)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Avatar not found")
    
    return StreamingResponse(
        stream_file(file_path),
        media_type="image/jpeg"
    )


@app.post("/api/delete-account")
async def delete_account(request: Request):
    """Delete current user's account"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("UPDATE messages SET deleted_for_sender = 1 WHERE sender_id = ?", (user["id"],))
        conn.execute("UPDATE messages SET deleted_for_recipient = 1 WHERE recipient_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.commit()
    
    return JSONResponse({"success": True})


# ========== THEME ENDPOINTS ===========

@app.get("/api/theme/tokens")
async def get_theme_tokens():
    """Get theme token manifest for dynamic editor generation"""
    return JSONResponse(THEME_TOKENS)


@app.get("/api/theme/presets")
async def get_theme_presets():
    """Get available theme presets (default, dark, etc.)"""
    return JSONResponse(THEME_PRESETS)


@app.get("/api/theme")
async def get_theme(request: Request):
    """Get current user's theme settings (v2 format with colors/images)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        row = conn.execute("SELECT theme_json FROM users WHERE id = ?", (user["id"],)).fetchone()
    
    # Merge with defaults and return v2 format
    theme_data = merge_theme_with_defaults(row["theme_json"] if row else None)
    return JSONResponse(theme_data)


@app.post("/api/theme")
async def save_theme(request: Request, theme_json: str = Form(...)):
    """Save theme settings for current user (expects v2 format)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("UPDATE users SET theme_json = ? WHERE id = ?", (theme_json, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True})


# ========== MESSAGE DELETE ENDPOINT ==========
@app.post("/api/delete-message")
async def delete_message_endpoint(request: Request, message_id: int = Form(...), mode: str = Form("self")):
    """Delete a message - either for self or for all (admin only)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        
        if msg["sender_id"] != user["id"] and msg["recipient_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Not authorized to delete this message")
        
        if mode == "all":
            if not user["is_admin"]:
                raise HTTPException(status_code=403, detail="Only admins can delete messages for all")
            conn.execute("UPDATE messages SET deleted_for_sender = 1, deleted_for_recipient = 1 WHERE id = ?", (message_id,))
        else:
            if msg["sender_id"] == user["id"]:
                conn.execute("UPDATE messages SET deleted_for_sender = 1 WHERE id = ?", (message_id,))
            else:
                conn.execute("UPDATE messages SET deleted_for_recipient = 1 WHERE id = ?", (message_id,))
        
        conn.commit()
    
    return JSONResponse({"success": True})


# ========== DELETE CHAT ENDPOINT ==========
@app.post("/api/delete-chat")
async def delete_chat_endpoint(request: Request, recipient_id: int = Form(...)):
    """Delete entire chat with a user (marks all messages as deleted for current user)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        conn.execute("""
            UPDATE messages 
            SET deleted_for_sender = 1 
            WHERE sender_id = ? AND recipient_id = ?
        """, (user["id"], recipient_id))
        
        conn.execute("""
            UPDATE messages 
            SET deleted_for_recipient = 1 
            WHERE sender_id = ? AND recipient_id = ?
        """, (recipient_id, user["id"]))
        
        conn.commit()
    
    return JSONResponse({"success": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
