import os
import re
import sqlite3
import hashlib
import secrets
import uuid
import json
import base64
import mimetypes
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote


import aiofiles
import aiofiles.os
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
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
        "select_border": {"key": "select_border", "css_var": "--select-border", "default": "#0084ff", "type": "color"},
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
    },
    # Phase 6.6b: sizing tokens - scale multiplier for command chips & toggle icon
    "sizing": {
        "chip_size": {"key": "chip_size", "css_var": "--chip-scale", "default": 1.0, "type": "range", "min": 0.8, "max": 1.3, "unit": "", "step": 0.05},
    }
}

# Phase 6.2: data-driven command registry (group commands).
# roles = caller roles allowed; "*" = any member.
# Phase 6.6: scope="dialog" commands run via POST /api/dialog/{uid}/command (1-on-1 only).
COMMAND_REGISTRY = [
    {"name": "add",     "args_hint": "@username", "roles": ["owner", "admin"],           "scope": "group",  "description": "Добавить участника в группу"},
    {"name": "kick",    "args_hint": "@username", "roles": ["owner", "admin"],           "scope": "group",  "description": "Исключить участника (матрица кика)"},
    {"name": "promote", "args_hint": "@username", "roles": ["owner"],                    "scope": "group",  "description": "Назначить групп-админом"},
    {"name": "rename",  "args_hint": "<имя>",     "roles": ["owner", "admin"],           "scope": "group",  "description": "Переименовать группу"},
    {"name": "leave",   "args_hint": "",          "roles": ["owner", "admin", "member"], "scope": "group",  "description": "Выйти из группы"},
    {"name": "help",    "args_hint": "",          "roles": ["*"],                        "scope": "group",  "description": "Показать доступные команды"},
    # Phase 6.6: dialog commands (no args - target is always the dialog peer)
    {"name": "block",   "args_hint": "",          "roles": ["*"],                        "scope": "dialog", "description": "Заблокировать собеседника"},
    {"name": "unblock", "args_hint": "",          "roles": ["*"],                        "scope": "dialog", "description": "Разблокировать собеседника"},
    {"name": "pin",     "args_hint": "",          "roles": ["*"],                        "scope": "dialog", "description": "Закрепить/открепить чат с собеседником"},
    # Phase 7.1d: mute/unmute notifications
    {"name": "mute",    "args_hint": "",          "roles": ["*"],                        "scope": "dialog", "description": "Заглушить уведомления от собеседника"},
    {"name": "unmute",  "args_hint": "",          "roles": ["*"],                        "scope": "dialog", "description": "Снять заглушку с собеседника"},
]


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
            "chip_cmd": "#dbe7ff"
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0},
        "sizing": {"chip_size": 1.0}
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
            "chip_cmd": "#2a3550",
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0},
        "sizing": {"chip_size": 1.0}
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
            "effects": {**THEME_PRESETS["default"].get("effects", {}), **theme.get("effects", {})},
            "sizing": {**THEME_PRESETS["default"].get("sizing", {}), **theme.get("sizing", {})}
        }
        return result
    except Exception as e:
        print(f"Theme merge error: {e}")
        return THEME_PRESETS["default"]


# Phase 6.3: live legacy color tokens that are NOT in the editor manifest
# (chip_cmd drives --chip-cmd-bg; border is auto-derived from bg, so not stored)
PRESET_EXTRA_COLORS = {"chip_cmd": "#dbe7ff"}
# Phase 6.3: built-in names can never be overwritten by user presets
PRESET_RESERVED_NAMES = {"default", "dark"}
PRESET_NAME_MAX_LEN = 50
THEME_IMPORT_MAX_BYTES = 256 * 1024


def sanitize_color_value(value, default):
    """Phase 6.3: keep only valid #rgb / #rrggbb strings, anything else -> default"""
    if not isinstance(value, str):
        return default
    v = value.strip().lstrip('#')
    if re.fullmatch(r'[0-9a-fA-F]{6}', v):
        return '#' + v.lower()
    m3 = re.fullmatch(r'[0-9a-fA-F]{3}', v)
    if m3:
        return '#' + ''.join(c * 2 for c in m3.group(0)).lower()
    return default


def sanitize_theme_config(raw):
    """
    Phase 6.3: validate an arbitrary theme config against the THEME_TOKENS manifest.
    Extra keys are dropped, missing/invalid values fall back to defaults.
    Image tokens are intentionally NOT portable: their uuids point at private
    uploads, so a foreign config must never grant access to someone's files.
    """
    raw = raw if isinstance(raw, dict) else {}
    raw_colors = raw.get("colors") if isinstance(raw.get("colors"), dict) else {}
    raw_effects = raw.get("effects") if isinstance(raw.get("effects"), dict) else {}
    raw_sizing = raw.get("sizing") if isinstance(raw.get("sizing"), dict) else {}

    colors = {}
    for key, spec in THEME_TOKENS["colors"].items():
        colors[key] = sanitize_color_value(raw_colors.get(key), spec["default"])
    for key, default in PRESET_EXTRA_COLORS.items():
        if key in raw_colors:
            colors[key] = sanitize_color_value(raw_colors[key], default)

    effects = {}
    for key, spec in THEME_TOKENS["effects"].items():
        try:
            n = int(float(raw_effects.get(key)))
        except (TypeError, ValueError):
            n = int(spec["default"])
        effects[key] = max(int(spec.get("min", 0)), min(int(spec.get("max", 20)), n))

    sizing = {}
    for key, spec in THEME_TOKENS.get("sizing", {}).items():
        try:
            v = float(raw_sizing.get(key))
        except (TypeError, ValueError):
            v = float(spec["default"])
        sizing[key] = max(float(spec.get("min", 0.5)), min(float(spec.get("max", 2)), v))

    return {"colors": colors, "images": {}, "effects": effects, "sizing": sizing}


def resolve_preset_name(explicit, parsed, fallback):
    """Phase 6.3: import name priority: form field > config "name" > filename.
    Reserved/empty names fall back to the generic one."""
    name = ""
    if isinstance(explicit, str) and explicit.strip():
        name = explicit.strip()
    elif isinstance(parsed, dict) and isinstance(parsed.get("name"), str) and parsed["name"].strip():
        name = parsed["name"].strip()
    elif fallback:
        name = Path(str(fallback)).stem.strip() or fallback
    if not name or len(name) > PRESET_NAME_MAX_LEN or name.lower() in PRESET_RESERVED_NAMES:
        name = "Импорт"
    return name


def load_user_presets(conn, user_id):
    """Phase 6.3: all custom theme presets of a user, newest first"""
    rows = conn.execute(
        "SELECT id, name, theme_json FROM theme_presets WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    result = []
    for r in rows:
        try:
            theme = json.loads(r["theme_json"])
        except Exception:
            theme = sanitize_theme_config(None)
        result.append({"id": r["id"], "name": r["name"], "theme": theme})
    return result


def upsert_user_preset(conn, user_id, name, theme):
    """Phase 6.3: save preset under a unique per-user name (re-save overwrites)"""
    existing = conn.execute(
        "SELECT id FROM theme_presets WHERE user_id = ? AND name = ?",
        (user_id, name)
    ).fetchone()
    payload = json.dumps(theme)
    if existing:
        conn.execute(
            "UPDATE theme_presets SET theme_json = ? WHERE id = ?",
            (payload, existing["id"])
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO theme_presets (user_id, name, theme_json) VALUES (?, ?, ?)",
        (user_id, name, payload)
    )
    return cur.lastrowid


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


# ========== Phase 7.1b: rate-limiting & security headers ==========

_login_failures: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 600  # 10 minutes in seconds
_RATE_LIMIT_MAX = 5       # max failures per window


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _record_failure(ip: str) -> int:
    now = time.time()
    if ip not in _login_failures:
        _login_failures[ip] = []
    _login_failures[ip].append(now)
    _login_failures[ip] = [t for t in _login_failures[ip] if now - t < _RATE_LIMIT_WINDOW]
    return len(_login_failures[ip])


def _clear_failures(ip: str):
    _login_failures.pop(ip, None)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src 'self' data: blob:; font-src 'self'"
    return response


# ========== Phase 7.1c: PULSE ring-buffer ==========
_pulse_buffer: deque = deque(maxlen=500)
_pulse_logger = logging.getLogger("pulse")
_login_logger = logging.getLogger("pulse.login")
_upload_logger = logging.getLogger("pulse.upload")
_ws_logger = logging.getLogger("pulse.ws")


def _pulse_emit(kind: str, detail: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    _pulse_buffer.append({"ts": ts, "kind": kind, "detail": detail})


@app.middleware("http")
async def pulse_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    path = request.url.path
    if path.startswith("/static") or path.startswith("/api/avatar"):
        return response
    _pulse_emit("http", f"{request.method} {path} → {response.status_code} ({elapsed_ms}ms)")
    return response


@app.get("/api/admin/pulse")
async def get_pulse(request: Request):
    user = get_current_user(request)
    require_admin(user)
    return JSONResponse(list(_pulse_buffer))


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
        
        # Phase 6.1: group support - NULL group_id = 1-on-1 dialog, set = group message
        if "group_id" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER NULL")
            print("Added column messages.group_id")
        
        # Phase 6.6: reply support (NULL = standalone message)
        if "reply_to_id" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN reply_to_id INTEGER NULL")
            print("Added column messages.reply_to_id")

        # Phase 7.2b: message editing (NULL = never edited)
        if "edited_at" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP NULL")
            print("Added column messages.edited_at")
        
        # Phase 6.6: per-user contact pins (pinned contacts float to the top)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                user_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, contact_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (contact_id) REFERENCES users(id)
            )
        """)
        
        # Phase 6.1: groups tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT DEFAULT 'member',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
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

        # Phase 6.3: user-defined theme presets (custom colors/effects configs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS theme_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                theme_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Phase 7.1a: email column for registration
        if "email" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NULL")
            print("Added column users.email")

        # Phase 7.1b: admin settings table (upload limits, etc.)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Seed defaults if missing
        for key, default in [("upload_rate_kbps", "0"), ("max_upload_mb", str(MAX_UPLOAD_BYTES // (1024 * 1024)))]:
            existing = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
            if not existing:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, default))

        # Phase 7.1c: admin audit log
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                action TEXT NOT NULL,
                target_id INTEGER,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Phase 7.1d: per-user mute list (suppress notifications for specific contacts)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                user_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, contact_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (contact_id) REFERENCES users(id)
            )
        """)

        # Phase 7.3: read receipts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reads (
                user_id INTEGER NOT NULL,
                peer_type TEXT NOT NULL DEFAULT 'user',
                peer_id INTEGER NOT NULL,
                last_read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, peer_type, peer_id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        msg_cols = {col[1] for col in conn.execute("PRAGMA table_info(messages)").fetchall()}
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


# ========== Phase 7.1a: auto-username from name ==========

_TRANSLIT_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def transliterate_name(name: str) -> str:
    """Transliterate Cyrillic name to latin/digits/_ (Phase 7.1a)"""
    result = []
    for ch in name.lower():
        if ch in _TRANSLIT_MAP:
            result.append(_TRANSLIT_MAP[ch])
        elif ch.isascii() and ch.isalnum():
            result.append(ch)
        elif ch in (' ', '-', '.'):
            result.append('_')
    return ''.join(result)


def generate_username(name: str) -> str:
    """Generate unique username from name: transliterate, fallback user_XXXX (Phase 7.1a)"""
    base = transliterate_name(name).strip('_')
    if not base or len(base) < 2:
        base = "user"
    # Ensure only valid chars
    base = re.sub(r'[^a-z0-9_]', '', base)
    if not base:
        base = "user"

    with get_db() as conn:
        # Try base first
        existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (base,)).fetchone()
        if not existing:
            return base
        # Collision: append random suffix
        for _ in range(20):
            candidate = f"{base}_{secrets.token_hex(2)}"
            existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone()
            if not existing:
                return candidate
        # Fallback
        return f"user_{secrets.token_hex(4)}"


def validate_email_format(email: str) -> bool:
    """Basic email format validation (Phase 7.1a)"""
    if not email or len(email) > 254:
        return False
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


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
async def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), invite_code: str = Form("")):
    import re
    
    ip = _get_client_ip(request)
    email_clean = email.strip().lower()
    
    if not validate_email_format(email_clean):
        count = _record_failure(ip)
        if count >= _RATE_LIMIT_MAX:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many attempts. Try again later."},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
            )
        return templates.TemplateResponse(request, "register.html", {
            "error": "Invalid email format",
            "invite_code": invite_code,
            "name": name,
            "email": email,
        })
    
    if len(password) < 4:
        count = _record_failure(ip)
        if count >= _RATE_LIMIT_MAX:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many attempts. Try again later."},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
            )
        return templates.TemplateResponse(request, "register.html", {
            "error": "Password must be at least 4 characters",
            "invite_code": invite_code,
            "name": name,
            "email": email,
        })
    
    password_hash = hash_password(password)
    username = generate_username(name)
    
    with get_db() as conn:
        existing_email = conn.execute("SELECT id FROM users WHERE email = ?", (email_clean,)).fetchone()
        if existing_email:
            count = _record_failure(ip)
            if count >= _RATE_LIMIT_MAX:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Try again later."},
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
                )
            return templates.TemplateResponse(request, "register.html", {
                "error": "Email already registered",
                "invite_code": invite_code,
                "name": name,
                "email": email,
            })
        
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = 1 if (count == 0 and FIRST_USER_ADMIN) else 0
        
        if count > 0 or not FIRST_USER_ADMIN:
            if not invite_code:
                count_f = _record_failure(ip)
                if count_f >= _RATE_LIMIT_MAX:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Too many attempts. Try again later."},
                        headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
                    )
                return templates.TemplateResponse(request, "register.html", {
                    "error": "Invite code is required",
                    "invite_code": invite_code,
                    "name": name,
                    "email": email,
                })
            
            invite = conn.execute("SELECT * FROM invites WHERE code = ? AND used_by IS NULL", (invite_code,)).fetchone()
            if not invite:
                count_f = _record_failure(ip)
                if count_f >= _RATE_LIMIT_MAX:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Too many attempts. Try again later."},
                        headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
                    )
                return templates.TemplateResponse(request, "register.html", {
                    "error": "Invalid or expired invite code",
                    "invite_code": invite_code,
                    "name": name,
                    "email": email,
                })
        
        conn.execute(
            "INSERT INTO users (name, username, password_hash, is_admin, email) VALUES (?, ?, ?, ?, ?)",
            (name, username, password_hash, is_admin, email_clean)
        )
        new_user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        if invite_code and count > 0:
            conn.execute("UPDATE invites SET used_by = ? WHERE code = ?", (new_user_id, invite_code))
        
        conn.commit()
    
    _clear_failures(ip)
    return RedirectResponse(url="/", status_code=303)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _get_client_ip(request)
    
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (username.strip().lower(),)).fetchone()
    
    if not user or not verify_password(password, user["password_hash"]):
        count = _record_failure(ip)
        _pulse_emit("login", f"FAIL ip={ip} email={username.strip().lower()} attempts={count}")
        if count >= _RATE_LIMIT_MAX:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many login attempts. Try again later."},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
            )
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid email or password"})
    
    _clear_failures(ip)
    _pulse_emit("login", f"OK ip={ip} user={user['username']} id={user['id']}")
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
        # Phase 6.6: pinned contacts float to the top (marker flag for the template)
        users = conn.execute("""
            SELECT u.id, u.name, u.username, u.avatar_uuid,
                   CASE WHEN p.contact_id IS NULL THEN 0 ELSE 1 END AS pinned
            FROM users u
            LEFT JOIN pins p ON p.contact_id = u.id AND p.user_id = ?
            WHERE u.id != ?
            ORDER BY pinned DESC, u.id ASC
        """, (user["id"], user["id"])).fetchall()
    
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
            SELECT m.id, m.sender_id, m.recipient_id, m.text, m.created_at, m.edited_at,
                   sender.avatar_uuid as sender_avatar_uuid,
                   m.reply_to_id,
                   r.text AS reply_to_text, ru.name AS reply_to_name
            FROM messages m
            JOIN users sender ON m.sender_id = sender.id
            LEFT JOIN messages r ON r.id = m.reply_to_id
            LEFT JOIN users ru ON ru.id = r.sender_id
            WHERE m.group_id IS NULL
              AND ((m.sender_id = ? AND m.recipient_id = ?) OR (m.sender_id = ? AND m.recipient_id = ?))
              -- Phase 6.5: hide rows this user deleted ("у себя" / "у всех" / delete-chat)
              AND ((m.sender_id = ? AND m.deleted_for_sender = 0) OR (m.recipient_id = ? AND m.deleted_for_recipient = 0))
            ORDER BY m.created_at ASC
        """, (user["id"], recipient_id, recipient_id, user["id"], user["id"], user["id"])).fetchall()
        
        # Phase 7.1d: include mute state for this dialog
        muted_by_me = conn.execute(
            "SELECT 1 FROM mutes WHERE user_id = ? AND contact_id = ?", (user["id"], recipient_id)
        ).fetchone() is not None
        
        # Build response with attachments and reactions for each message
        result = []
        for msg in messages:
            msg_dict = dict(msg)
            if msg_dict.get("reply_to_id"):
                msg_dict["reply_to_text"] = clip_reply_snippet(msg_dict.get("reply_to_text"))
            attachments = conn.execute(
                "SELECT id, uuid_name, orig_name, mime, size, created_at FROM attachments WHERE message_id = ?",
                (msg_dict["id"],)
            ).fetchall()
            msg_dict["attachments"] = [dict(a) for a in attachments]
            result.append(msg_dict)
    
    return JSONResponse({"messages": result, "muted_by_me": muted_by_me})


# ============== Phase 6.1: Groups ==============

def get_group_membership(group_id: int, user_id: int):
    """Return dict {user_id, group_id, role, joined_at} if user is a member of the group, else None"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT group_id, user_id, role, joined_at FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def get_effective_group_role(user, group_id: int):
    """Phase 6.2: resolve caller's effective role in a group.
    Returns (role, grp): role is 'owner'|'admin'|'member';
    grp=None only when the GROUP does not exist; role=None with grp set = not a member."""
    with get_db() as conn:
        grp = conn.execute("SELECT id, name, owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not grp:
            return None, None
        mem = conn.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user["id"])
        ).fetchone()
    grp_dict = dict(grp)
    if not mem:
        return None, grp_dict  # group exists, caller is not a member -> 403 upstream
    if grp["owner_id"] == user["id"]:
        return "owner", grp_dict
    if user.get("is_admin"):
        return "admin", grp_dict  # platform admin acts as group admin
    return mem["role"], grp_dict


def kick_allowed(actor, grp_owner_id: int, actor_role: str, target_role: str, target_uid: int):
    """Phase 6.2 shared kick matrix: self=leave; owner kicks anyone;
    group-admin/platform-admin kick only plain members."""
    if target_uid == actor["id"]:
        return True
    if actor["id"] == grp_owner_id:
        return True
    if actor_role == "admin":
        return target_role == "member"
    if actor.get("is_admin"):
        return target_role == "member"
    return False


@app.post("/api/groups")
async def create_group(request: Request, name: str = Form("")):
    """Create a group; creator becomes owner and first member"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    name = (name or "").strip()
    if not name or len(name) > 50:
        raise HTTPException(status_code=400, detail="Group name must be 1-50 characters")
    
    with get_db() as conn:
        conn.execute("INSERT INTO groups (name, owner_id) VALUES (?, ?)", (name, user["id"]))
        group_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'owner')",
            (group_id, user["id"])
        )
        conn.commit()
    
    return JSONResponse({"id": group_id, "name": name})


@app.get("/api/groups")
async def list_groups(request: Request):
    """Groups the current user belongs to (id, name, member count)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        rows = conn.execute("""
            SELECT g.id, g.name, g.owner_id, COUNT(gm.user_id) AS member_count
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE g.id IN (SELECT group_id FROM group_members WHERE user_id = ?)
            GROUP BY g.id, g.name, g.owner_id
            ORDER BY g.created_at ASC, g.id ASC
        """, (user["id"],)).fetchall()
    
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/groups/{group_id}/members")
async def list_group_members(request: Request, group_id: int):
    """Member list with roles - only for group members"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not get_group_membership(group_id, user["id"]):
        raise HTTPException(status_code=403, detail="Not a group member")
    
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.id, u.name, u.username, u.avatar_uuid, gm.role, gm.joined_at
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
            ORDER BY gm.joined_at ASC, u.id ASC
        """, (group_id,)).fetchall()
    
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/groups/{group_id}/members")
async def add_group_member(request: Request, group_id: int, user_id: int = Form(...)):
    """Add a member - allowed for the group owner or platform admin; no duplicates"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        grp = conn.execute("SELECT id, owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not grp:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if grp["owner_id"] != user["id"] and not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Only the group owner can add members")
        
        target = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        
        dupe = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        ).fetchone()
        if dupe:
            raise HTTPException(status_code=400, detail="Already a member")
        
        conn.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'member')",
            (group_id, user_id)
        )
        conn.commit()
    
    # Phase 6.1b: notify online members so their sidebar picks up the new member
    with get_db() as conn:
        member_rows = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
    for row in member_rows:
        uid = row["user_id"]
        ws_conn = app.state.connections.get(uid)
        if ws_conn is not None and uid != user["id"]:
            try:
                await ws_conn.send_json({"type": "group_added", "group_id": group_id})
            except Exception:
                pass  # Connection might have closed
    
    return JSONResponse({"success": True})


@app.delete("/api/groups/{group_id}/members/{user_id}")
async def remove_group_member(request: Request, group_id: int, user_id: int):
    """Kick/leave: owner kicks anyone; admin kicks only plain 'member'; self-removal = leave"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not get_group_membership(group_id, user["id"]):
        raise HTTPException(status_code=403, detail="Not a group member")
    
    with get_db() as conn:
        grp = conn.execute("SELECT id, owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not grp:
            raise HTTPException(status_code=404, detail="Group not found")
        
        target = conn.execute(
            "SELECT user_id, role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id)
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Not a member")
        
        # Permission matrix: self-leave always OK; owner kicks anyone;
        # group/platform admin kicks only role='member'; everyone else forbidden
        my_mem = get_group_membership(group_id, user["id"])
        if not kick_allowed(user, grp["owner_id"], my_mem["role"], target["role"], user_id):
            raise HTTPException(status_code=403, detail="Forbidden")
        
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        conn.commit()
    
    return JSONResponse({"success": True})


@app.get("/api/groups/{group_id}/messages")
async def group_messages(request: Request, group_id: int):
    """Group chat history - only for members; includes sender name/username/avatar and attachments"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not get_group_membership(group_id, user["id"]):
        raise HTTPException(status_code=403, detail="Not a group member")
    
    with get_db() as conn:
        messages = conn.execute("""
            SELECT m.id, m.sender_id, m.recipient_id, m.group_id, m.text, m.created_at, m.edited_at,
                   u.name AS sender_name, u.username AS sender_username,
                   u.avatar_uuid AS sender_avatar_uuid,
                   m.reply_to_id,
                   r.text AS reply_to_text, ru.name AS reply_to_name
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            LEFT JOIN messages r ON r.id = m.reply_to_id
            LEFT JOIN users ru ON ru.id = r.sender_id
            WHERE m.group_id = ?
              -- Phase 6.5: group deletion is self-only, so hide a message just for
              -- the member who deleted their own copy (everyone else still sees it)
              AND (m.deleted_for_sender = 0 OR m.sender_id != ?)
            ORDER BY m.created_at ASC, m.id ASC
        """, (group_id, user["id"])).fetchall()
        
        result = []
        for msg in messages:
            msg_dict = dict(msg)
            if msg_dict.get("reply_to_id"):
                msg_dict["reply_to_text"] = clip_reply_snippet(msg_dict.get("reply_to_text"))
            attachments = conn.execute(
                "SELECT id, uuid_name, orig_name, mime, size, created_at FROM attachments WHERE message_id = ?",
                (msg_dict["id"],)
            ).fetchall()
            msg_dict["attachments"] = [dict(a) for a in attachments]
            result.append(msg_dict)
    
    return JSONResponse(result)


# ===== Phase 7.3: read receipts =====
@app.post("/api/read")
async def mark_read(request: Request):
    """Mark a chat as read up to now."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    peer_type = body.get("type", "user")
    peer_id = body.get("id")
    if not peer_id:
        raise HTTPException(status_code=400, detail="Missing id")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reads (user_id, peer_type, peer_id, last_read_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, peer_type, peer_id) DO UPDATE SET last_read_at = ?",
            (user["id"], peer_type, peer_id, now, now)
        )
        conn.commit()
    return JSONResponse({"ok": True})


@app.get("/api/unread")
async def get_unread(request: Request):
    """Get unread counts for all contacts and groups."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = []
    with get_db() as conn:
        # Dialogs: find all users I've exchanged messages with
        peers = conn.execute("""
            SELECT DISTINCT
                CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END AS peer_id
            FROM messages
            WHERE (sender_id = ? OR recipient_id = ?) AND group_id IS NULL
        """, (user["id"], user["id"], user["id"])).fetchall()
        for p in peers:
            pid = p["peer_id"]
            row = conn.execute("SELECT last_read_at FROM reads WHERE user_id = ? AND peer_type = 'user' AND peer_id = ?",
                               (user["id"], pid)).fetchone()
            last_read = row["last_read_at"] if row else "1970-01-01 00:00:00"
            count = conn.execute("""
                SELECT COUNT(*) AS c FROM messages
                WHERE sender_id = ? AND recipient_id = ? AND group_id IS NULL
                  AND created_at > ? AND deleted_for_recipient = 0
            """, (pid, user["id"], last_read)).fetchone()["c"]
            if count > 0:
                result.append({"type": "user", "id": pid, "count": count})
        # Groups: find all groups I'm a member of
        groups = conn.execute("SELECT group_id FROM group_members WHERE user_id = ?", (user["id"],)).fetchall()
        for g in groups:
            gid = g["group_id"]
            row = conn.execute("SELECT last_read_at FROM reads WHERE user_id = ? AND peer_type = 'group' AND peer_id = ?",
                               (user["id"], gid)).fetchone()
            last_read = row["last_read_at"] if row else "1970-01-01 00:00:00"
            count = conn.execute("""
                SELECT COUNT(*) AS c FROM messages
                WHERE group_id = ? AND sender_id != ? AND created_at > ? AND deleted_for_sender = 0 AND deleted_for_recipient = 0
            """, (gid, user["id"], last_read)).fetchone()["c"]
            if count > 0:
                result.append({"type": "group", "id": gid, "count": count})
    return JSONResponse(result)


# ===== Phase 6.2: group commands =====

@app.get("/api/commands")
async def api_commands(request: Request, group_id: int = 0):
    """Commands available to the caller in the given context (role-aware).
    Phase 6.6: group_id<=0 returns dialog-scope commands for 1-on-1 chats."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if group_id <= 0:
        # Phase 6.6: dialog chips - same for every authed user (target is the open peer)
        return JSONResponse([
            dict(c) for c in COMMAND_REGISTRY
            if c["scope"] == "dialog"
        ])
    
    role, _grp = get_effective_group_role(user, group_id)
    if not role:
        return []  # no chips for non-members (no info leak)
    
    return JSONResponse([
        dict(c) for c in COMMAND_REGISTRY
        if c["scope"] == "group" and ("*" in c["roles"] or role in c["roles"])
    ])


# ===== Phase 6.6: dialog commands (/block /unblock /pin) =====

def get_dialog_state(user_id, peer_id):
    """Per-user dialog state vs a peer: my pin + block matrix + mute (both directions)."""
    with get_db() as conn:
        pinned = conn.execute(
            "SELECT 1 FROM pins WHERE user_id = ? AND contact_id = ?", (user_id, peer_id)
        ).fetchone() is not None
        blocked_by_me = conn.execute(
            "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (user_id, peer_id)
        ).fetchone() is not None
        blocked_me = conn.execute(
            "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (peer_id, user_id)
        ).fetchone() is not None
        # Phase 7.1d: muted_by_me — I suppressed notifications from this contact
        muted_by_me = conn.execute(
            "SELECT 1 FROM mutes WHERE user_id = ? AND contact_id = ?", (user_id, peer_id)
        ).fetchone() is not None
    return {"pinned": pinned, "blocked_by_me": blocked_by_me, "blocked_me": blocked_me, "muted_by_me": muted_by_me}


@app.get("/api/dialog/{uid}/state")
async def dialog_state(request: Request, uid: int):
    """State snapshot for the dialog chips (pin/block labels)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if uid == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot target yourself")
    
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            raise HTTPException(status_code=404, detail="User not found")
    
    return JSONResponse(get_dialog_state(user["id"], uid))


@app.post("/api/dialog/{uid}/command")
async def dialog_command(request: Request, uid: int, cmd: str = Form(...)):
    """Execute a dialog command against the peer. ALL checks happen server-side."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if uid == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot target yourself")
    
    with get_db() as conn:
        peer = conn.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    if not peer:
        raise HTTPException(status_code=404, detail="User not found")
    
    cmd_clean = (cmd or "").strip().lstrip("/").lower()
    spec = next((c for c in COMMAND_REGISTRY if c["name"] == cmd_clean and c["scope"] == "dialog"), None)
    if not spec:
        raise HTTPException(status_code=400, detail=f"Неизвестная команда диалога: /{cmd_clean}")
    
    with get_db() as conn:
        if cmd_clean == "block":
            already = conn.execute(
                "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], uid)
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
                    (user["id"], uid)
                )
                conn.commit()
                return JSONResponse({"ok": True, "text": f"@{peer['username']} заблокирован", "state": get_dialog_state(user["id"], uid)})
            return JSONResponse({"ok": True, "text": "Уже заблокирован", "state": get_dialog_state(user["id"], uid)})
        
        if cmd_clean == "unblock":
            cur = conn.execute(
                "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], uid)
            )
            conn.commit()
            text = "Разблокирован" if cur.rowcount else "И не был заблокирован"
            return JSONResponse({"ok": True, "text": text, "state": get_dialog_state(user["id"], uid)})
        
        if cmd_clean == "pin":
            existing = conn.execute(
                "SELECT 1 FROM pins WHERE user_id = ? AND contact_id = ?", (user["id"], uid)
            ).fetchone()
            if existing:
                conn.execute("DELETE FROM pins WHERE user_id = ? AND contact_id = ?", (user["id"], uid))
                conn.commit()
                return JSONResponse({"ok": True, "text": "Чат откреплён", "pinned": False, "state": get_dialog_state(user["id"], uid)})
            conn.execute(
                "INSERT OR IGNORE INTO pins (user_id, contact_id) VALUES (?, ?)", (user["id"], uid)
            )
            conn.commit()
            return JSONResponse({"ok": True, "text": "Чат закреплён", "pinned": True, "state": get_dialog_state(user["id"], uid)})
        
        # Phase 7.1d: /mute — suppress notifications from this contact
        if cmd_clean == "mute":
            already = conn.execute(
                "SELECT 1 FROM mutes WHERE user_id = ? AND contact_id = ?", (user["id"], uid)
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT OR IGNORE INTO mutes (user_id, contact_id) VALUES (?, ?)",
                    (user["id"], uid)
                )
                conn.commit()
                return JSONResponse({"ok": True, "text": f"Уведомления от @{peer['username']} отключены", "state": get_dialog_state(user["id"], uid)})
            return JSONResponse({"ok": True, "text": "Уже заглушен", "state": get_dialog_state(user["id"], uid)})
        
        # Phase 7.1d: /unmute — restore notifications
        if cmd_clean == "unmute":
            cur = conn.execute(
                "DELETE FROM mutes WHERE user_id = ? AND contact_id = ?", (user["id"], uid)
            )
            conn.commit()
            text = "Уведомления восстановлены" if cur.rowcount else "И не был заглушен"
            return JSONResponse({"ok": True, "text": text, "state": get_dialog_state(user["id"], uid)})
    
    raise HTTPException(status_code=400, detail="Неизвестная команда")


def _notify_group_changed(group_id: int, exclude_uid=None):
    """Phase 6.2 helper: tell online members to refresh their sidebar (counts/names)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
    return [
        (row["user_id"], app.state.connections.get(row["user_id"]))
        for row in rows
        if row["user_id"] != exclude_uid and app.state.connections.get(row["user_id"]) is not None
    ]


@app.post("/api/groups/{group_id}/command")
async def group_command(request: Request, group_id: int, cmd: str = Form(""), args: str = Form("")):
    """Execute a group command. ALL role checks happen here; client only displays the result."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    role, grp = get_effective_group_role(user, group_id)
    if not grp:
        raise HTTPException(status_code=404, detail="Group not found")
    if not role:
        raise HTTPException(status_code=403, detail="Not a group member")
    
    cmd_clean = (cmd or "").strip().lstrip("/").lower()
    # Phase 6.6: scope filter - dialog commands (/block /unblock /pin) never run in groups
    spec = next((c for c in COMMAND_REGISTRY if c["name"] == cmd_clean and c["scope"] == "group"), None)
    if not spec:
        raise HTTPException(status_code=400, detail=f"Неизвестная команда: /{cmd_clean}. /help — список")
    
    if not ("*" in spec["roles"] or role in spec["roles"]):
        raise HTTPException(status_code=403, detail=f"Команда /{cmd_clean} недоступна для вашей роли")
    
    args_s = (args or "").strip()
    
    # ---- /help ---------------------------------------------------------
    if cmd_clean == "help":
        lines = [
            f"/{c['name']}" + (f" {c['args_hint']}" if c["args_hint"] else "") + f" — {c['description']}"
            for c in COMMAND_REGISTRY if c["scope"] == "group" and ("*" in c["roles"] or role in c["roles"])
        ]
        return JSONResponse({"ok": True, "text": "Доступные команды:\n" + "\n".join(lines)})
    
    # ---- commands below need a @username target --------------------------
    if cmd_clean in ("add", "kick", "promote"):
        uname = args_s.lstrip("@").strip()
        if not uname:
            raise HTTPException(status_code=400, detail=f"Укажите @username: /{cmd_clean} {spec['args_hint']}")
        
        with get_db() as conn:
            target_user = conn.execute(
                "SELECT id, username FROM users WHERE username = ?", (uname,)
            ).fetchone()
            if not target_user:
                raise HTTPException(status_code=400, detail=f"Пользователь @{uname} не найден")
            
            target_mem = conn.execute(
                "SELECT user_id, role FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, target_user["id"])
            ).fetchone()
        
        if cmd_clean == "add":
            if target_mem:
                raise HTTPException(status_code=400, detail=f"@{uname} уже состоит в группе")
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'member')",
                    (group_id, target_user["id"])
                )
                conn.commit()
            # live sidebar refresh for everyone (the added member now resolves too)
            for uid, ws_conn in _notify_group_changed(group_id):
                try:
                    await ws_conn.send_json({"type": "group_added", "group_id": group_id})
                except Exception:
                    pass
            return JSONResponse({"ok": True, "text": f"@{uname} добавлен в группу «{grp['name']}»"})
        
        if cmd_clean == "kick":
            if not target_mem:
                raise HTTPException(status_code=400, detail=f"@{uname} не состоит в группе")
            if not kick_allowed(user, grp["owner_id"], role, target_mem["role"], target_user["id"]):
                raise HTTPException(status_code=403, detail=f"Недостаточно прав, чтобы исключить @{uname}")
            left_self = target_user["id"] == user["id"]
            with get_db() as conn:
                conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                             (group_id, target_user["id"]))
                conn.commit()
            for uid, ws_conn in _notify_group_changed(group_id):
                try:
                    await ws_conn.send_json({"type": "group_added", "group_id": group_id})
                except Exception:
                    pass
            if left_self:
                return JSONResponse({"ok": True, "text": f"Вы покинули группу «{grp['name']}»"})
            return JSONResponse({"ok": True, "text": f"@{uname} исключён из группы"})
        
        if cmd_clean == "promote":
            if not target_mem:
                raise HTTPException(status_code=400, detail=f"@{uname} не состоит в группе")
            if target_mem["role"] != "member":
                raise HTTPException(status_code=400, detail=f"@{uname} уже не обычный участник")
            with get_db() as conn:
                conn.execute("UPDATE group_members SET role = 'admin' WHERE group_id = ? AND user_id = ?",
                             (group_id, target_user["id"]))
                conn.commit()
            return JSONResponse({"ok": True, "text": f"@{uname} теперь групп-админ"})
    
    # ---- /leave -----------------------------------------------------------
    if cmd_clean == "leave":
        with get_db() as conn:
            conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                         (group_id, user["id"]))
            conn.commit()
        for uid, ws_conn in _notify_group_changed(group_id):
            try:
                await ws_conn.send_json({"type": "group_added", "group_id": group_id})
            except Exception:
                pass
        return JSONResponse({"ok": True, "text": f"Вы покинули группу «{grp['name']}»"})
    
    # ---- /rename ------------------------------------------------------------
    if cmd_clean == "rename":
        new_name = args_s.strip()
        if not new_name or len(new_name) > 50:
            raise HTTPException(status_code=400, detail="Имя группы должно быть от 1 до 50 символов")
        with get_db() as conn:
            conn.execute("UPDATE groups SET name = ? WHERE id = ?", (new_name, group_id))
            conn.commit()
        for uid, ws_conn in _notify_group_changed(group_id):
            try:
                await ws_conn.send_json({"type": "group_added", "group_id": group_id})
            except Exception:
                pass
        return JSONResponse({"ok": True, "text": f"Группа переименована в «{new_name}»"})
    
    raise HTTPException(status_code=400, detail="Команда не реализована")


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
    
    # Parse session to get user_id
    # Phase 6.1 fix: SessionMiddleware (starlette 0.36) signs the cookie with a
    # TimestampSigner, not URLSafeTimedSerializer - the old parse rejected EVERY
    # websocket with 4001, so live delivery never worked. Mirror the middleware:
    from itsdangerous import TimestampSigner, BadSignature
    try:
        signed = TimestampSigner(str(SECRET_KEY)).unsign(session_data, max_age=14 * 24 * 60 * 60)
        session = json.loads(base64.b64decode(signed))
        user_id = session.get("user_id")
    except (BadSignature, Exception):
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
    _pulse_emit("ws", f"CONNECT user_id={user_id}")
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            # Phase 7.3: typing indicator — relay to participants
            if msg_type == "typing":
                group_id = data.get("group_id")
                with get_db() as conn:
                    sender_row = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
                sender_name = sender_row["name"] if sender_row else "User"
                payload = {"type": "typing", "user_id": user_id, "name": sender_name}
                if group_id:
                    payload["group_id"] = group_id
                    with get_db() as conn:
                        members = conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)).fetchall()
                    for m in members:
                        if m["user_id"] == user_id:
                            continue
                        ws_conn = app.state.connections.get(m["user_id"])
                        if ws_conn:
                            try: await ws_conn.send_json(payload)
                            except: pass
                else:
                    peer_id = data.get("peer_id")
                    if peer_id:
                        ws_conn = app.state.connections.get(peer_id)
                        if ws_conn:
                            try: await ws_conn.send_json(payload)
                            except: pass
            # Phase 7.3: mark as read via WS
            elif msg_type == "read":
                peer_type = data.get("peer_type", "user")
                peer_id = data.get("peer_id")
                if peer_id:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with get_db() as conn:
                        conn.execute(
                            "INSERT INTO reads (user_id, peer_type, peer_id, last_read_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(user_id, peer_type, peer_id) DO UPDATE SET last_read_at = ?",
                            (user_id, peer_type, peer_id, now, now)
                        )
                        conn.commit()
    except WebSocketDisconnect:
        _pulse_emit("ws", f"DISCONNECT user_id={user_id}")
        if user_id in app.state.connections:
            del app.state.connections[user_id]
    except Exception:
        _pulse_emit("ws", f"DISCONNECT user_id={user_id} (error)")
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


REPLY_SNIPPET_MAX = 140


def clip_reply_snippet(text):
    """Phase 6.6: quote snippets are capped identically everywhere."""
    s = (text or "").strip()
    if len(s) > REPLY_SNIPPET_MAX:
        s = s[:REPLY_SNIPPET_MAX].rstrip() + "…"
    return s


def reply_payload(reply_to_id):
    """Phase 6.6: quote fields for a message payload (empty dict = no reply).
    Text is truncated server-side so both history and WS stay consistent."""
    if not reply_to_id:
        return {}
    with get_db() as conn:
        row = conn.execute("""
            SELECT m.text, m.sender_id, u.name
            FROM messages m JOIN users u ON u.id = m.sender_id
            WHERE m.id = ?
        """, (reply_to_id,)).fetchone()
    if not row:
        return {"reply_to_id": int(reply_to_id), "reply_to_text": "", "reply_to_name": ""}
    return {"reply_to_id": int(reply_to_id), "reply_to_text": clip_reply_snippet(row["text"]), "reply_to_name": row["name"] or ""}


@app.post("/api/send")
async def send_message(
    request: Request,
    recipient_id: int = Form(0),
    group_id: int = Form(0),
    text: str = Form(""),
    reply_to_id: int = Form(0),
    files: list[UploadFile] = File(default=[])
):
    user = get_current_user_fresh(request)  # Use fresh data to catch ban status
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Check if user is banned
    if is_banned(user):
        banned_until = datetime.fromisoformat(user["banned_until"].replace("Z", "+00:00").replace("+00:00", ""))
        raise HTTPException(status_code=403, detail=f"You are banned until {banned_until.strftime('%Y-%m-%d %H:%M')}")
    
    # Phase 6.1: group_id set -> group message (recipient_id forced to 0, blocks skipped in MVP)
    is_group = group_id > 0
    if is_group:
        with get_db() as conn:
            grp = conn.execute("SELECT id FROM groups WHERE id = ?", (group_id,)).fetchone()
            if not grp:
                raise HTTPException(status_code=404, detail="Group not found")
            membership = conn.execute(
                "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user["id"])
            ).fetchone()
        if not membership:
            raise HTTPException(status_code=403, detail="Not a group member")
        recipient_id = 0
    else:
        # Check if there's a block between users (1-on-1 only)
        if check_block(user["id"], recipient_id):
            raise HTTPException(status_code=403, detail="Communication is blocked")
    
    # Allow empty text only if there are attachments
    if not text.strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message")
    
    if len(text) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Message too long")
    
    with get_db() as conn:
        if not is_group:
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
        
        # Phase 6.6: reply validation - target must exist in the SAME conversation
        reply_target = None
        if reply_to_id and reply_to_id > 0:
            if is_group:
                reply_target = conn.execute(
                    "SELECT id, sender_id, text FROM messages WHERE id = ? AND group_id = ?",
                    (reply_to_id, group_id)
                ).fetchone()
            else:
                reply_target = conn.execute("""
                    SELECT id, sender_id, text FROM messages
                    WHERE id = ? AND group_id IS NULL
                      AND ((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))
                """, (reply_to_id, user["id"], recipient_id, recipient_id, user["id"])).fetchone()
            if not reply_target:
                raise HTTPException(status_code=404, detail="Сообщение для ответа не найдено")
        
        conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, text, group_id, reply_to_id) VALUES (?, ?, ?, ?, ?)",
            (user["id"], recipient_id, text, group_id if is_group else None, reply_target["id"] if reply_target else None)
        )
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Handle file uploads with chunked streaming (64KB chunks)
        attachment_ids = []
        # Phase 7.1b-fix: read upload limits from settings ON EVERY REQUEST
        with get_db() as settings_conn:
            rate_row = settings_conn.execute("SELECT value FROM settings WHERE key = 'upload_rate_kbps'").fetchone()
            max_mb_row = settings_conn.execute("SELECT value FROM settings WHERE key = 'max_upload_mb'").fetchone()
        upload_rate_kbps = int(rate_row["value"]) if rate_row else 0
        max_upload_mb = int(max_mb_row["value"]) if max_mb_row else (MAX_UPLOAD_BYTES // (1024 * 1024))
        effective_max_bytes = max_upload_mb * 1024 * 1024
        # budget per 64KB chunk in seconds; rate=0 → no sleep
        chunk_bytes = 64 * 1024
        chunk_budget = (chunk_bytes / (upload_rate_kbps * 1024)) if upload_rate_kbps > 0 else 0

        for file in files:
            if file.filename:
                # Phase 7.1b: pre-check file size hint if available
                if file.size and file.size > effective_max_bytes:
                    raise HTTPException(status_code=413, detail=f"File '{file.filename}' exceeds {max_upload_mb} MB limit")

                # Generate UUID filename with extension
                ext = Path(file.filename).suffix.lower()
                uuid_name = f"{uuid.uuid4().hex}{ext}"
                file_path = os.path.join(UPLOADS_DIR, uuid_name)
                
                # Stream file to disk in 64KB chunks while counting bytes
                total_bytes = 0
                async with aiofiles.open(file_path, 'wb') as f:
                    while True:
                        chunk_start = time.monotonic()
                        chunk = await file.read(chunk_bytes)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > effective_max_bytes:
                            await aiofiles.os.remove(file_path)
                            raise HTTPException(status_code=413, detail=f"File '{file.filename}' exceeds {max_upload_mb} MB limit")
                        await f.write(chunk)
                        # Phase 7.1b-fix: token-bucket – sleep = budget − elapsed
                        if chunk_budget > 0:
                            elapsed = time.monotonic() - chunk_start
                            sleep_time = chunk_budget - elapsed
                            if sleep_time > 0:
                                await asyncio.sleep(sleep_time)
                
                # Detect mime type from extension
                mime_type = mimetypes.guess_type(file.filename)[0] or file.content_type or "application/octet-stream"
                
                # Store attachment in DB
                conn.execute(
                    "INSERT INTO attachments (message_id, uuid_name, orig_name, mime, size) VALUES (?, ?, ?, ?, ?)",
                    (message_id, uuid_name, file.filename, mime_type, total_bytes)
                )
                attachment_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                _pulse_emit("upload", f"file={file.filename} size={total_bytes} mime={mime_type}")
        
        conn.commit()
        
        # Get the inserted message with attachments
        msg = conn.execute(
            "SELECT id, sender_id, recipient_id, group_id, text, created_at, reply_to_id FROM messages WHERE id = ?",
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
    # Phase 6.6: reply payload for both clients (sender + WS receivers)
    msg_dict.update(reply_payload(msg_dict.get("reply_to_id")))
    
    if is_group:
        # Phase 6.1: group message carries sender identity so receivers can render name/avatar
        msg_dict["sender_name"] = user.get("name") or ""
        msg_dict["sender_username"] = user.get("username") or ""
        msg_dict["sender_avatar_uuid"] = user.get("avatar_uuid") or ""
        
        # Fan out via WebSocket to all online group members except the sender
        with get_db() as conn:
            member_rows = conn.execute(
                "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
            ).fetchall()
        for row in member_rows:
            uid = row["user_id"]
            if uid == user["id"]:
                continue
            ws_conn = app.state.connections.get(uid)
            if ws_conn is not None:
                try:
                    await ws_conn.send_json(msg_dict)
                except Exception:
                    pass  # Connection might have closed
    elif recipient_id in app.state.connections:
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
            SELECT a.id, a.uuid_name, a.orig_name, a.mime, a.size, m.sender_id, m.recipient_id, m.group_id
            FROM attachments a
            JOIN messages m ON a.message_id = m.id
            WHERE a.id = ?
        """, (attachment_id,)).fetchone()
        
        if not att:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        # Phase 6.1: group attachments are for group members only; dialogs keep participant check
        if att["group_id"]:
            if not get_group_membership(att["group_id"], user["id"]):
                raise HTTPException(status_code=403, detail="Forbidden")
        elif user["id"] != att["sender_id"] and user["id"] != att["recipient_id"]:
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
            SELECT a.id, a.uuid_name, a.orig_name, a.mime, a.size, a.created_at, m.sender_id, m.recipient_id, m.group_id
            FROM attachments a
            JOIN messages m ON a.message_id = m.id
            WHERE a.id = ?
        """, (attachment_id,)).fetchone()
        
        if not att:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        # Phase 6.1: group attachments are for group members only; dialogs keep participant check
        if att["group_id"]:
            if not get_group_membership(att["group_id"], user["id"]):
                raise HTTPException(status_code=403, detail="Forbidden")
        elif user["id"] != att["sender_id"] and user["id"] != att["recipient_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
    
    return JSONResponse(dict(att))


# ============== Phase 3: Admin Panel, Invites, Bans, Blocks ==============

def require_admin(user):
    """Check if user is admin, raise 403 if not"""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


def log_admin_action(actor_id, action, target_id=None, detail=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admin_log (actor_id, action, target_id, detail) VALUES (?, ?, ?, ?)",
            (actor_id, action, target_id, detail)
        )
        conn.commit()


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
            log_admin_action(user["id"], "warn+auto_ban", target_user_id, f"reason={reason}; banned until {banned_until.strftime('%Y-%m-%d')}")
            return JSONResponse({"warn_count": warn_count, "auto_banned": True, "banned_until": banned_until.isoformat()})
        
        conn.commit()
    
    log_admin_action(user["id"], "warn", target_user_id, f"reason={reason}; count={warn_count}")
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
    
    log_admin_action(user["id"], "ban", target_user_id, f"days={days}; until {banned_until.strftime('%Y-%m-%d')}")
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
    
    log_admin_action(user["id"], "unban", target_user_id)
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
    
    log_admin_action(user["id"], "grant_admin", target_user_id)
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
    
    log_admin_action(user["id"], "revoke_admin", target_user_id)
    return JSONResponse({"success": True})


# ========== Phase 7.1b: admin settings (upload limits) ==========

@app.get("/api/admin/settings")
async def get_admin_settings(request: Request):
    user = get_current_user(request)
    require_admin(user)
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return JSONResponse({r["key"]: r["value"] for r in rows})


@app.post("/api/admin/settings")
async def save_admin_settings(request: Request):
    user = get_current_user(request)
    require_admin(user)
    body = await request.json()
    allowed = {"upload_rate_kbps", "max_upload_mb"}
    with get_db() as conn:
        for k, v in body.items():
            if k in allowed:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        conn.commit()
    return JSONResponse({"success": True})


# ========== Phase 7.1c: admin audit log ==========

@app.get("/api/admin/audit")
async def get_admin_audit(request: Request):
    user = get_current_user(request)
    require_admin(user)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT a.id, a.created_at, u.username AS actor, a.action,
                   t.username AS target, a.detail
            FROM admin_log a
            LEFT JOIN users u ON a.actor_id = u.id
            LEFT JOIN users t ON a.target_id = t.id
            ORDER BY a.id DESC
            LIMIT 100
        """).fetchall()
    return JSONResponse([dict(r) for r in rows])


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
        row = conn.execute("SELECT id, name, username, email, avatar_uuid, bio FROM users WHERE id = ?", (user["id"],)).fetchone()
    
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


# ========== Phase 7.1a: username / email / password changes ==========

@app.post("/api/profile/username")
async def change_username(request: Request, username: str = Form(...), password: str = Form(...)):
    """Change current user's username (requires current password for confirmation)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    username_clean = username.strip().lstrip('@')
    if not re.match(r'^[a-zA-Z0-9_]+$', username_clean) or len(username_clean) < 3:
        raise HTTPException(status_code=400, detail="Username must be 3+ chars: Latin letters, digits, underscore only")
    
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect password")
    
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ? AND id != ?", (username_clean, user["id"])).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Username already taken")
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (username_clean, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True, "username": username_clean})


@app.post("/api/profile/email")
async def change_email(request: Request, email: str = Form(...), password: str = Form(...)):
    """Change current user's email (requires current password)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    email_clean = email.strip().lower()
    if not validate_email_format(email_clean):
        raise HTTPException(status_code=400, detail="Invalid email format")
    
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect password")
    
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email_clean, user["id"])).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (email_clean, user["id"]))
        conn.commit()
    
    return JSONResponse({"success": True, "email": email_clean})


@app.post("/api/profile/password")
async def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...), email_code: str = Form("")):
    """Change current user's password.
    If email is set, email_code must match the email address.
    If email is NULL, only current password is required (with a hint)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    # Email verification step
    if user.get("email"):
        if not email_code:
            raise HTTPException(status_code=400, detail="Email confirmation required: enter your email address")
        if email_code.strip().lower() != user["email"]:
            raise HTTPException(status_code=400, detail="Email does not match")
    
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    
    new_hash = hash_password(new_password)
    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
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
        # Phase 6.3: drop the account's custom theme presets along with it
        conn.execute("DELETE FROM theme_presets WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.commit()
    
    log_admin_action(user["id"], "delete_self", user["id"], "self-deletion")
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


# ========== CUSTOM THEME PRESETS (Phase 6.3) ==========

@app.get("/api/theme/presets/custom")
async def get_custom_presets(request: Request):
    """List current user's saved theme presets"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with get_db() as conn:
        presets = load_user_presets(conn, user["id"])

    return JSONResponse({"presets": presets})


@app.post("/api/theme/presets/custom")
async def save_custom_preset(request: Request, name: str = Form(...), theme_json: str = Form(...)):
    """Save current editor state as a named preset (validated against the manifest)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required")
    if len(name) > PRESET_NAME_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"Preset name too long (max {PRESET_NAME_MAX_LEN})")
    if name.lower() in PRESET_RESERVED_NAMES:
        raise HTTPException(status_code=400, detail="This name is reserved for built-in presets")

    try:
        parsed = json.loads(theme_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid theme JSON")

    clean = sanitize_theme_config(parsed)
    with get_db() as conn:
        preset_id = upsert_user_preset(conn, user["id"], name, clean)
        conn.commit()

    return JSONResponse({"id": preset_id, "name": name})


@app.delete("/api/theme/presets/custom/{preset_id}")
async def delete_custom_preset(preset_id: int, request: Request):
    """Delete one of the current user's presets (foreign ids are not reachable)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM theme_presets WHERE id = ? AND user_id = ?",
            (preset_id, user["id"])
        )
        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Preset not found")

    return JSONResponse({"success": True})


@app.post("/api/theme/presets/import")
async def import_theme_preset(request: Request, file: UploadFile = File(...), name: str = Form("")):
    """Import a shared JSON theme config - it becomes a new custom preset.
    Keys are validated against the manifest: extra keys dropped, invalid
    values fall back to defaults, so a broken/hostile file can never crash."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw = await file.read()
    if len(raw) > THEME_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"File too large (max {THEME_IMPORT_MAX_BYTES // 1024} KB)")

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    clean = sanitize_theme_config(parsed)
    preset_name = resolve_preset_name(name, parsed, file.filename)

    with get_db() as conn:
        preset_id = upsert_user_preset(conn, user["id"], preset_name, clean)
        conn.commit()

    return JSONResponse({"id": preset_id, "name": preset_name})


@app.get("/api/theme/export")
async def export_theme(request: Request):
    """Download current theme as a portable JSON config (colors + effects).
    Re-importable via /api/theme/presets/import; images are binary uploads,
    so they are not part of the portable config."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with get_db() as conn:
        row = conn.execute("SELECT theme_json FROM users WHERE id = ?", (user["id"],)).fetchone()

    merged = merge_theme_with_defaults(row["theme_json"] if row else None)
    payload = {
        "format": "vibe-theme",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "colors": merged.get("colors", {}),
        "effects": merged.get("effects", {}),
        "sizing": merged.get("sizing", {})
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="vibe-theme-{stamp}.json"'}
    )


# ========== MESSAGE EDIT ENDPOINT ==========
@app.post("/api/messages/{message_id}/edit")
async def edit_message(message_id: int, request: Request, text: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db() as conn:
        msg = conn.execute("SELECT id, sender_id, group_id, recipient_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg["sender_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only author can edit")
        if not text.strip():
            raise HTTPException(status_code=400, detail="Empty message")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE messages SET text = ?, edited_at = ? WHERE id = ?", (text, now, message_id))
        conn.commit()
    # broadcast to participants
    participants = set()
    if msg["group_id"]:
        with get_db() as conn:
            rows = conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (msg["group_id"],)).fetchall()
            participants = {r["user_id"] for r in rows}
    else:
        participants = {msg["sender_id"], msg["recipient_id"]}
    for uid in participants:
        ws_conn = app.state.connections.get(uid)
        if ws_conn:
            try:
                await ws_conn.send_json({
                    "type": "message_edited", "message_id": message_id,
                    "text": text, "edited_at": now,
                    "group_id": msg["group_id"], "sender_id": msg["sender_id"],
                    "recipient_id": msg["recipient_id"]
                })
            except Exception:
                pass
    return JSONResponse({"ok": True, "edited_at": now})


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
        
        # Phase 7.2b: group messages — check membership instead of recipient_id
        if msg["group_id"]:
            membership = conn.execute(
                "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
                (msg["group_id"], user["id"])
            ).fetchone()
            is_sender = msg["sender_id"] == user["id"]
            is_admin = bool(user["is_admin"])
            if not membership:
                raise HTTPException(status_code=403, detail="Not a group member")
            # non-admin can only delete own messages
            if not is_sender and not is_admin:
                raise HTTPException(status_code=403, detail="Only own messages or admin")
        else:
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
