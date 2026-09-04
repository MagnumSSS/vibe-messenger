import os
import re
import sys
import sqlite3
import hashlib
import hmac
import secrets
import smtplib
import ssl
import uuid
from email.message import EmailMessage
import json
import base64
import mimetypes
import time
import asyncio
import logging
import shutil
from logging.handlers import RotatingFileHandler
from collections import deque
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, parse_qs


import aiofiles
import aiofiles.os
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles 

# ========== Phase R5: .env подхватывается ДО чтения любых настроек ==========
# Явные переменные окружения всегда важнее файла (load_env их не перезаписывает).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import load_env  # noqa: E402  (импорт после настройки sys.path — так и задумано)

load_env.load()

# Config from env
APP_MODE = (os.environ.get("APP_MODE") or "dev").strip().lower()
SECRET_KEY = (os.environ.get("SECRET_KEY") or "").strip()
# Phase R4: сам ключ в логи не пишем никогда — только факт/длину
DEV_FALLBACK_KEY = "dev-secret-key-change-in-production"
FIRST_USER_ADMIN = os.environ.get("FIRST_USER_ADMIN", "1") == "1"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "10485760"))  # 10MB default
THEME_IMAGE_MAX_BYTES = int(os.environ.get("THEME_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))  # ~5MB for theme image slots (separate from MAX_UPLOAD_BYTES)
PORT = int(os.environ.get("PORT", "8000"))
# Phase R1: DATA_DIR is the single source of truth for all persistent state.
# DB and uploads are derived from it, tests may point it at a temp dir.
DATA_DIR = os.environ.get("DATA_DIR", "data")


# ========== Phase R5: guard DATA_DIR ==========
# Проверяем ДО логирования и БД: если каталог недоступен, дальше нет смысла идти.
def guard_data_dir(path: str) -> None:
    """Каталог данных должен существовать (или создаваться) и быть доступен для записи."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"FATAL: DATA_DIR={path!r} не удалось создать: {exc}\n")
        sys.exit(1)
    if not os.path.isdir(path):
        sys.stderr.write(f"FATAL: DATA_DIR={path!r} не является каталогом\n")
        sys.exit(1)
    probe = os.path.join(path, ".write-test")
    try:
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        sys.stderr.write(f"FATAL: DATA_DIR={path!r} недоступен для записи: {exc}\n")
        sys.exit(1)


guard_data_dir(DATA_DIR)

DB_PATH = os.path.join(DATA_DIR, "messenger.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
THEME_IMAGES_DIR = os.path.join(DATA_DIR, "theme_images")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(THEME_IMAGES_DIR, exist_ok=True)


# ========== Phase R4: логи ==========
# Всё пишется в data/logs (data/ целиком в .gitignore), формат:
#   время | level | модуль | сообщение
# Секреты (cookie, SECRET_KEY, пароли) в логи не попадают — см. УСТАВ, п. 12.
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(module)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 МБ на файл
LOG_BACKUP_COUNT = 5              # app.log.1 … app.log.5
APP_LOG_PATH = os.path.join(LOG_DIR, "app.log")
ERROR_LOG_PATH = os.path.join(LOG_DIR, "error.log")


def setup_logging(level=logging.INFO) -> None:
    """
    Настроить логи один раз на процесс:
      data/logs/app.log   — INFO и выше, ротация 5 МБ × 5
      data/logs/error.log — только ERROR и выше, своя ротация
      stdout              — дубль app.log, чтобы dev-режим остался видимым
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    root = logging.getLogger("messenger")
    root.setLevel(level)
    for handler in list(root.handlers):     # повторный вызов не плодит хендлеры
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    app_handler = RotatingFileHandler(
        APP_LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    error_handler = RotatingFileHandler(
        ERROR_LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    root.propagate = False


setup_logging()

app_logger = logging.getLogger("messenger.app")
error_logger = logging.getLogger("messenger.error")
admin_logger = logging.getLogger("messenger.admin")

# ========== Phase R5: режимы (dev|prod) и строгость к SECRET_KEY ==========
if APP_MODE not in ("dev", "prod"):
    app_logger.warning("APP_MODE=%r неизвестен — работаем как dev", APP_MODE)
    APP_MODE = "dev"

if APP_MODE == "prod":
    if not SECRET_KEY or SECRET_KEY == "localdev" or len(SECRET_KEY) < 32:
        error_logger.error(
            "APP_MODE=prod требует SECRET_KEY >= 32 символов "
            "(сейчас: %s); сгенерируй: python -c \"import secrets;print(secrets.token_hex(32))\"",
            "не задан" if not SECRET_KEY else f"{len(SECRET_KEY)} символов"
        )
        sys.exit(1)
    app_logger.info("APP_MODE=prod, SECRET_KEY задан (%d символов)", len(SECRET_KEY))
elif not SECRET_KEY:
    SECRET_KEY = DEV_FALLBACK_KEY
    app_logger.warning("dev-режим: SECRET_KEY не задан — используется небезопасный ключ разработки")
elif SECRET_KEY == "localdev":
    app_logger.warning("dev-режим: SECRET_KEY=localdev — небезопасный ключ dev-режима")
elif len(SECRET_KEY) < 32:
    app_logger.warning("dev-режим: слабый SECRET_KEY (%d символов) — для прода непригоден", len(SECRET_KEY))
else:
    app_logger.info("dev-режим: SECRET_KEY задан (%d символов)", len(SECRET_KEY))

# uptime считается от момента импорта приложения
START_TIME = time.monotonic()
# результат стартовой проверки целостности (для /api/admin/pulse)
LAST_INTEGRITY: dict = {"ok": None, "detail": None, "ts": None}
# метки времени необработанных ошибок — по ним считается errors_last_hour
_error_timestamps: deque = deque()

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
        # Phase 7.8: цвет острова шапки (glass сохраняется), цвет ника, цвета присутствия
        "header_color": {"key": "header_color", "css_var": "--header-island-color", "default": "#ffffff", "type": "color"},
        "name_color": {"key": "name_color", "css_var": "--name-color", "default": "#0084ff", "type": "color"},
        "presence_online": {"key": "presence_online", "css_var": "--presence-online", "default": "#2ecc71", "type": "color"},
        "presence_offline": {"key": "presence_offline", "css_var": "--presence-offline", "default": "#9aa0a6", "type": "color"},
        "presence_text": {"key": "presence_text", "css_var": "--presence-text", "default": "#666666", "type": "color"},
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
    },
    # Phase 7.8: бинарные настройки темы (тумблеры в редакторе)
    "toggles": {
        "name_color_auto": {"key": "name_color_auto", "default": True, "type": "toggle",
                            "label": "Цвет ника: авто-контраст из темы"},
        "peer_banner_header": {"key": "peer_banner_header", "default": False, "type": "toggle",
                               "label": "Баннер собеседника в шапке"},
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
            "chip_cmd": "#dbe7ff",
            # Phase 7.8
            "header_color": "#ffffff",
            "name_color": "#0084ff",
            "presence_online": "#2ecc71",
            "presence_offline": "#9aa0a6",
            "presence_text": "#666666"
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0},
        "sizing": {"chip_size": 1.0},
        "toggles": {"name_color_auto": True, "peer_banner_header": False}
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
            # Phase 7.8: остров шапки — тёмный, ник — светлый (YIQ-автоконтраст)
            "header_color": "#16213e",
            "name_color": "#eaeaea",
            "presence_online": "#2ecc71",
            "presence_offline": "#8b8f94",
            "presence_text": "#a0a0a0"
        },
        "images": {},
        "effects": {"wallpaper_blur": 0, "bubble_blur": 0},
        "sizing": {"chip_size": 1.0},
        "toggles": {"name_color_auto": True, "peer_banner_header": False}
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
            "sizing": {**THEME_PRESETS["default"].get("sizing", {}), **theme.get("sizing", {})},
            # Phase 7.8: тумблеры темы
            "toggles": {**THEME_PRESETS["default"].get("toggles", {}), **theme.get("toggles", {})}
        }
        return result
    except Exception as e:
        app_logger.warning("тема: не удалось разобрать theme_json: %s", e)
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
    raw_toggles = raw.get("toggles") if isinstance(raw.get("toggles"), dict) else {}

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

    # Phase 7.8: тумблеры — только известные ключи и только булевы значения
    toggles = {}
    for key, spec in THEME_TOKENS.get("toggles", {}).items():
        value = raw_toggles.get(key, spec["default"])
        toggles[key] = bool(value) if isinstance(value, (bool, int)) else bool(spec["default"])

    return {"colors": colors, "images": {}, "effects": effects, "sizing": sizing, "toggles": toggles}


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


# Phase R6 (A05): debug-режим (трассировки в ответе) — только в dev, в prod всегда выключен
app = FastAPI(debug=(APP_MODE == "dev"))

# Формат сессионной cookie настраивается из env, чтобы приложение работало
# и напрямую, и внутри кросс-сайтового iframe (предпросмотр в браузере):
#   SESSION_SAME_SITE=lax|strict|none   (default lax)
#   SESSION_SECURE=1                    (default 0) — ставит флаг Secure
# Для iframe на другом домене нужно none + secure=1: иначе браузер cookie
# просто не отправляет, и после логина /chat снова кидает на /.
SESSION_SAME_SITE = os.environ.get("SESSION_SAME_SITE", "lax").lower()
if SESSION_SAME_SITE not in ("lax", "strict", "none"):
    SESSION_SAME_SITE = "lax"
SESSION_SECURE = os.environ.get("SESSION_SECURE", "0") == "1"
# X-Frame-Options: deny (по умолчанию) | sameorigin | none — для предпросмотра нужно none
FRAME_OPTIONS = os.environ.get("FRAME_OPTIONS", "deny").lower()
if FRAME_OPTIONS not in ("deny", "sameorigin", "none"):
    FRAME_OPTIONS = "deny"

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site=SESSION_SAME_SITE,
    https_only=SESSION_SECURE,
)
app_logger.info(
    "сессия: cookie same_site=%s secure=%s; X-Frame-Options=%s",
    SESSION_SAME_SITE, SESSION_SECURE, FRAME_OPTIONS
)


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


# ========== Phase R6 (A01/CSRF): double-submit токен ==========
# Клиент присылает значение cookie csrftoken в заголовке X-CSRF-Token
# (для HTML-форм и multipart — в поле формы csrf_token). Cookie НЕ HttpOnly:
# фронтенд должен уметь прочитать своё же значение. В кросс-сайтовом iframe
# режим (SameSite=None; Secure) задаётся теми же env, что и сессия.
CSRF_COOKIE = "csrftoken"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_MAX_AGE = 14 * 24 * 3600      # как у сессии: две недели


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _csrf_token_from_body(content_type: str, body: bytes) -> str:
    """Значение поля csrf_token из тела urlencoded-формы или multipart (без разбора всей формы)."""
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        except Exception:
            return ""
        values = parsed.get(CSRF_FIELD) or []
        return values[0] if values else ""
    if content_type.startswith("multipart/form-data"):
        marker = b'name="' + CSRF_FIELD.encode() + b'"'
        idx = body.find(marker)
        if idx == -1:
            return ""
        start = body.find(b"\r\n\r\n", idx)
        if start == -1:
            return ""
        start += 4
        end = body.find(b"\r\n", start)
        return body[start:end if end != -1 else len(body)].decode("utf-8", "replace").strip()
    return ""


def _csrf_cookie_header(token: str) -> str:
    """Set-Cookie для csrftoken: НЕ HttpOnly — фронтенд читает своё же значение."""
    parts = [f"{CSRF_COOKIE}={token}", "Path=/", f"Max-Age={CSRF_MAX_AGE}"]
    if SESSION_SAME_SITE:
        parts.append(f"SameSite={SESSION_SAME_SITE.capitalize()}")
    if SESSION_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


class CSRFMiddleware:
    """
    Чистый ASGI-мидлварь (не BaseHTTPMiddleware): читаем тело, достаём токен
    и ОТДАЁМ ЕГО ОБРАТНО приложению — иначе эндпоинт получил бы пустое тело
    (BaseHTTPMiddleware не делится уже прочитанным стримом).
    """

    # тело больше лимита аплоада (+1 МБ на границы multipart) не буферизуем
    BODY_LIMIT = MAX_UPLOAD_BYTES + 1024 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_dict = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        cookie_token = ""
        for chunk in headers_dict.get("cookie", "").split("; "):
            if chunk.startswith(CSRF_COOKIE + "="):
                cookie_token = chunk[len(CSRF_COOKIE) + 1:]
                break
        token = cookie_token or _new_csrf_token()
        # request.state живёт в scope["state"] — шаблоны берут {{ request.state.csrf_token }}
        scope.setdefault("state", {})["csrf_token"] = token

        method = scope.get("method", "GET").upper()
        if method in CSRF_SAFE_METHODS:
            if not cookie_token:
                await self.app(scope, receive, self._send_with_cookie(send, token))
            else:
                await self.app(scope, receive, send)
            return

        # --- изменяющий запрос: проверяем токен (из заголовка или из тела) ---
        sent_token = headers_dict.get(CSRF_HEADER, "")
        buffered: bytes | None = None
        if not sent_token:
            # заголовка нет — читаем тело и ищем поле csrf_token
            body = b""
            more_body = True
            while more_body:
                message = await receive()
                body += message.get("body", b"")
                more_body = message.get("more_body", False)
                if len(body) > self.BODY_LIMIT:
                    await self._reject(send, status=413, detail="Payload too large")
                    return
            buffered = body
            sent_token = _csrf_token_from_body(headers_dict.get("content-type", ""), body)

        if not cookie_token or not sent_token or not hmac.compare_digest(cookie_token, sent_token):
            admin_logger.warning(
                "CSRF отклонён: %s %s (client=%s)", method, scope.get("path", "?"),
                (scope.get("client") or ["?"])[0]
            )
            await self._reject(send, status=403, detail="CSRF token missing or invalid")
            return

        if buffered is None:
            # тело не читали — приложение получит оригинальный receive
            downstream_receive = receive
        else:
            # тело отдаём РОВНО ОДИН раз: дальше прокидываем оригинальный receive,
            # иначе StreamingResponse дождётся «второго» http.request и упадёт
            replayed = False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": buffered, "more_body": False}
                return await receive()

            downstream_receive = replay

        if not cookie_token:
            await self.app(scope, downstream_receive, self._send_with_cookie(send, token))
        else:
            await self.app(scope, downstream_receive, send)

    @staticmethod
    async def _reject(send, status: int, detail: str):
        payload = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    def _send_with_cookie(send, token: str):
        cookie = _csrf_cookie_header(token).encode("latin-1")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("set-cookie", cookie.decode("latin-1"))
            await send(message)

        return send_wrapper


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if FRAME_OPTIONS == "deny":
        response.headers["X-Frame-Options"] = "DENY"
    elif FRAME_OPTIONS == "sameorigin":
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # FRAME_OPTIONS=none — заголовок не шлём (нужно для предпросмотра в iframe)
    response.headers["Referrer-Policy"] = "no-referrer"
    # Phase R6 (A05): запрещаем браузеру отдавать сайту гео/камеру/микрофон и т.п.
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
        "accelerometer=(), gyroscope=(), magnetometer=()"
    )
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src 'self' data: blob:; font-src 'self'"
    # Phase micro banner3: no-cache for all /api/* responses
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ========== Phase 7.1c: PULSE ring-buffer ==========
_pulse_buffer: deque = deque(maxlen=500)
_pulse_logger = logging.getLogger("messenger.pulse")
_login_logger = logging.getLogger("messenger.pulse.login")
_upload_logger = logging.getLogger("messenger.pulse.upload")
_ws_logger = logging.getLogger("messenger.pulse.ws")


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
    """События (ring buffer) + метрики состояния (фаза R4)."""
    user = get_current_user(request)
    require_admin(user)
    return JSONResponse({"events": list(_pulse_buffer), "metrics": pulse_metrics()})


def pulse_metrics() -> dict:
    """Метрики для админки: диск, БД, uptime, WS, ошибки за час, целостность, бэкап."""
    now = time.time()
    while _error_timestamps and _error_timestamps[0] < now - 3600:
        _error_timestamps.popleft()

    try:
        disk_free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        disk_free = 0
    try:
        db_size = os.path.getsize(DB_PATH)
    except OSError:
        db_size = 0

    return {
        "disk_free_bytes": disk_free,
        "db_size_bytes": db_size,
        "uptime_seconds": round(time.monotonic() - START_TIME, 1),
        "ws_connections": len(app.state.connections),
        "errors_last_hour": len(_error_timestamps),
        "last_integrity": dict(LAST_INTEGRITY),
        "last_backup_at": last_backup_at(),
    }


def last_backup_at():
    """mtime самой свежей копии в BACKUP_DIR; None, если каталога нет или он пуст."""
    backup_dir = os.environ.get("BACKUP_DIR", "/var/lib/messenger/backups")
    try:
        files = [os.path.join(backup_dir, name) for name in os.listdir(backup_dir)]
        files = [path for path in files if os.path.isfile(path)]
    except OSError:
        return None
    if not files:
        return None
    try:
        return datetime.fromtimestamp(max(os.path.getmtime(p) for p in files)).isoformat(timespec="seconds")
    except OSError:
        return None


@app.middleware("http")
async def error_logging_middleware(request: Request, call_next):
    """
    Phase R4: необработанное исключение → error.log с traceback, клиенту 500.
    В лог уходят только метод, путь и хост — без cookie, заголовков и тела.
    """
    try:
        return await call_next(request)
    except Exception:
        _error_timestamps.append(time.time())
        client = request.client.host if request.client else "?"
        error_logger.exception("необработанное исключение: %s %s (client=%s)",
                               request.method, request.url.path, client)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


@app.api_route("/api/admin/debug-boom", methods=["GET", "POST"])
async def debug_boom(request: Request):
    """
    Phase R4: намеренный сбой для проверки error.log (только админ, только dev).
    Клиент обязан получить 500, а traceback — лечь в data/logs/error.log.
    """
    user = get_current_user(request)
    require_admin(user)
    # Phase R6 (A05): в prod намеренный генератор ошибок недоступен даже админу
    if APP_MODE == "prod":
        raise HTTPException(status_code=404, detail="Not Found")
    raise RuntimeError("DEBUG BOOM: намеренное исключение для проверки логов")


templates = Jinja2Templates(directory="templates")


def get_conn() -> sqlite3.Connection:
    """
    Phase R2: единственная точка создания соединений с SQLite.
    Ни один модуль не вызывает sqlite3.connect напрямую — только здесь
    выставляются PRAGMA, критичные для целостности и параллельной работы.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")    # ждём чужую блокировку, вместо «database is locked»
    conn.execute("PRAGMA foreign_keys = ON")      # целостность ссылок обязательна
    conn.execute("PRAGMA synchronous = NORMAL")   # разумный компромисс для WAL
    return conn


@contextmanager
def get_db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()




# ===================================================================== #
# Phase 7.6d-fix: канал объявлений «ВайбБункер»
# ===================================================================== #
# Канал — это НЕ пользователь:
#   * в users нет никаких ботов (служебный @start удалён миграцией);
#   * профиль канала лежит в settings (broadcast_*), правит его только creator;
#   * сообщения канала — обычные строки messages с peer_type='broadcast',
#     peer_id=0 и sender_id = реальный создатель канала.

# Как выглядела служебная строка канала в версии 7.6d — по ней чистим старые базы
LEGACY_START_USERNAME = "start"
LEGACY_START_PASSWORD_HASH = "!system-account-no-login!"

CHANNEL_NAME_DEFAULT = "ВайбБункер"     # имя канала по умолчанию
CHANNEL_SUBTITLE = "канал объявлений"   # подпись под именем в списке
CHANNEL_MARKER = "📢"                   # маркер канала
CHANNEL_PEER_TYPE = "broadcast"         # значение messages.peer_type для объявлений
PEER_TYPE_USER = "user"
PEER_TYPE_GROUP = "group"

# Профиль канала по умолчанию. Ключи — единственные разрешённые в settings.
CHANNEL_SETTINGS_DEFAULTS = {
    "broadcast_name": CHANNEL_NAME_DEFAULT,
    "broadcast_bio": "",
    "broadcast_avatar_uuid": "",
    "broadcast_banner_uuid": "",
}

# Шаблон первого объявления: кнопка «Вставить шаблон» у creator в пустом канале.
WELCOME_TEMPLATE = (
    "Добро пожаловать в ВайбБункер! 👋\n\n"
    "• Профиль и темы: аватар, баннер, био, своя тема — шестерёнка сверху.\n\n"
    "• Сообщения: свайп по сообщению — ответ; чипы «Редакт»/«Удалить» — режимы; "
    "долгое нажатие — меню.\n\n"
    "• Контакт: потяни строку контакта («занавес») — пин, мьют, удаление чата.\n\n"
    "• Группы: «+» в разделе Группы; команды начинаются с «/».\n\n"
    "• Почта: в профиле — подтверждение почты и смена пароля по коду.\n\n"
    "• PWA: «Установить» в меню браузера — приложение без вкладок.\n\n"
    "Это канал объявлений — ответы отключены."
)


def get_channel_profile(conn) -> dict:
    """Профиль канала из settings (broadcast_*), с дефолтами для пустой БД."""
    profile = dict(CHANNEL_SETTINGS_DEFAULTS)
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except Exception:
        rows = []
    for row in rows:
        key = row["key"]
        if key in profile:
            profile[key] = row["value"]
    return profile


def channel_payload(conn) -> dict:
    """Публичное описание канала для шаблона и API."""
    profile = get_channel_profile(conn)
    name = (profile.get("broadcast_name") or "").strip() or CHANNEL_NAME_DEFAULT
    return {
        "id": 0,
        "is_channel": True,
        "peer_type": CHANNEL_PEER_TYPE,
        "name": name,
        "subtitle": CHANNEL_SUBTITLE,
        "marker": CHANNEL_MARKER,
        "bio": profile.get("broadcast_bio") or "",
        "avatar_uuid": profile.get("broadcast_avatar_uuid") or "",
        "banner_uuid": profile.get("broadcast_banner_uuid") or "",
    }


def set_channel_fields(conn, fields: dict) -> None:
    """Записать настройки канала. Ключи — только из CHANNEL_SETTINGS_DEFAULTS (A03)."""
    for key, value in fields.items():
        if key not in CHANNEL_SETTINGS_DEFAULTS:
            raise ValueError(f"недопустимый ключ настроек канала: {key!r}")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def creator_user_id(conn) -> int | None:
    """id создателя: помеченный is_creator, иначе самый первый живой пользователь."""
    row = conn.execute("SELECT id FROM users WHERE is_creator = 1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    row = conn.execute("SELECT MIN(id) AS id FROM users").fetchone()
    if row and row["id"] is not None:
        return int(row["id"])
    return None


def is_creator_user(user) -> bool:
    return bool(user) and int(user.get("is_creator") or 0) == 1


def require_creator(user) -> None:
    """Писать, править и удалять в канале может только создатель (админы — читатели)."""
    if not is_creator_user(user):
        raise HTTPException(
            status_code=403,
            detail="Канал объявлений: писать и оформлять может только создатель",
        )


def normalize_read_peer_type(peer_type: str) -> str:
    """Фронт шлёт type='channel' для отметки «прочитано» — внутри это 'broadcast'."""
    if peer_type in ("channel", CHANNEL_PEER_TYPE):
        return CHANNEL_PEER_TYPE
    return peer_type or PEER_TYPE_USER


# ---------- миграции 7.6d-fix (вызываются из ensure_schema) ---------- #

def _migrate_channel_columns(conn) -> None:
    """Проставить peer_type/peer_id существующим сообщениям (группы и 1-на-1)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "peer_type" not in cols or "peer_id" not in cols:
        return
    cur = conn.execute(
        "UPDATE messages SET peer_type = ?, peer_id = COALESCE(group_id, 0) "
        "WHERE (group_id IS NOT NULL AND group_id != 0) AND peer_type = ?",
        (PEER_TYPE_GROUP, PEER_TYPE_USER),
    )
    if cur.rowcount:
        app_logger.info("миграция каналов: перемаркировано group-сообщений: %s", cur.rowcount)
    cur = conn.execute(
        "UPDATE messages SET peer_id = recipient_id "
        "WHERE peer_type = ? AND (group_id IS NULL OR group_id = 0) "
        "AND peer_id = 0 AND recipient_id > 0",
        (PEER_TYPE_USER,),
    )
    if cur.rowcount:
        app_logger.info("миграция каналов: перемаркировано 1-на-1 сообщений: %s", cur.rowcount)


def _remove_system_user(conn) -> int:
    """
    Служебный бот @start больше не нужен: удаляем его и всё, что на него ссылалось.
    Помечен он был флагом is_system, но старые базы могли дойти без колонки —
    тогда системную строку узнаём по связке username + служебный password_hash.
    """
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM users WHERE is_system = 1 "
        "OR (username = ? AND password_hash = ?)",
        (LEGACY_START_USERNAME, LEGACY_START_PASSWORD_HASH),
    ).fetchall()]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM attachments WHERE message_id IN "
        f"(SELECT id FROM messages WHERE sender_id IN ({ph}) OR recipient_id IN ({ph}))",
        ids + ids,
    )
    conn.execute(
        f"DELETE FROM messages WHERE sender_id IN ({ph}) OR recipient_id IN ({ph})", ids + ids
    )
    conn.execute(f"DELETE FROM pins WHERE user_id IN ({ph}) OR contact_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM mutes WHERE user_id IN ({ph}) OR contact_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM blocks WHERE blocker_id IN ({ph}) OR blocked_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM reads WHERE user_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM group_members WHERE user_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM theme_presets WHERE user_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM email_codes WHERE user_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM invites WHERE created_by IN ({ph})", ids)
    conn.execute(f"DELETE FROM warns WHERE user_id IN ({ph}) OR by_admin_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM users WHERE id IN ({ph})", ids)
    app_logger.info("миграция каналов: служебный контакт «start» удалён: id=%s", ids)
    return len(ids)


def _ensure_creator(conn) -> None:
    """creator = первый живой пользователь (min id), если флаг ещё никому не выставлен."""
    if conn.execute("SELECT 1 FROM users WHERE is_creator = 1 LIMIT 1").fetchone():
        return
    row = conn.execute("SELECT MIN(id) AS id FROM users").fetchone()
    if row and row["id"] is not None:
        conn.execute("UPDATE users SET is_creator = 1 WHERE id = ?", (int(row["id"]),))
        app_logger.info("миграция каналов: creator назначен: user_id=%s", int(row["id"]))


def _seed_channel_settings(conn) -> None:
    """Профиль канала живёт в settings — создадим ключи, если их ещё нет."""
    for key, default in CHANNEL_SETTINGS_DEFAULTS.items():
        exists = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
        if not exists:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, default))


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
            ("font_scale", "REAL DEFAULT 1.0"),      # Phase 7 micro: a11y font scale
            ("banner_uuid", "TEXT NULL"),            # Phase 7.6a: profile banner
            ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),  # Phase R6: смена пароля рвёт чужие сессии
            ("email_verified", "INTEGER NOT NULL DEFAULT 0"),  # Phase R7: почта подтверждена кодом
            ("is_system", "INTEGER NOT NULL DEFAULT 0"),   # Phase 7.6d: системный канал «start»
            ("is_creator", "INTEGER NOT NULL DEFAULT 0"),  # Phase 7.6d: первый зарегистрированный
            ("last_seen", "TIMESTAMP NULL"),           # Phase 7.8: присутствие
        ]
        for col_name, col_type in user_column_additions:
            if col_name not in user_columns:
                # A03: имя колонки берём только из whitelist-константы выше
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name):
                    raise ValueError(f"недопустимое имя колонки: {col_name!r}")
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                app_logger.info(f"схема: добавлена колонка users.{col_name}")
        
        # Create messages table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- Phase R2: FK только на sender_id. recipient_id = 0 для групповых
                -- сообщений (служебный sentinel), поэтому ссылкой он быть не может.
                FOREIGN KEY (sender_id) REFERENCES users(id)
            )
        """)
        
        # Get current columns in messages table
        msg_columns = {col[1] for col in conn.execute("PRAGMA table_info(messages)").fetchall()}
        
        # Add missing columns to messages table (Phase 4: soft delete)
        msg_column_additions = [
            ("deleted_for_sender", "INTEGER DEFAULT 0"),
            ("deleted_for_recipient", "INTEGER DEFAULT 0"),
            # Phase 7.6d-fix: 'user' | 'group' | 'broadcast' + адресат (0 для канала)
            ("peer_type", "TEXT NOT NULL DEFAULT 'user'"),
            ("peer_id", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col_name, col_type in msg_column_additions:
            if col_name not in msg_columns:
                # A03: имя колонки берём только из whitelist-константы выше
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name):
                    raise ValueError(f"недопустимое имя колонки: {col_name!r}")
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
                app_logger.info(f"схема: добавлена колонка messages.{col_name}")
        
        # Phase 6.1: group support - NULL group_id = 1-on-1 dialog, set = group message
        if "group_id" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER NULL")
            app_logger.info("схема: добавлена колонка messages.group_id")
        
        # Phase 6.6: reply support (NULL = standalone message)
        if "reply_to_id" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN reply_to_id INTEGER NULL")
            app_logger.info("схема: добавлена колонка messages.reply_to_id")

        # Phase 7.2b: message editing (NULL = never edited)
        if "edited_at" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP NULL")
            app_logger.info("схема: добавлена колонка messages.edited_at")
        
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
            app_logger.info("схема: добавлена колонка users.email")

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

        # Phase R7: коды подтверждения по почте (plaintext кода не хранится — только sha256)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                target TEXT NULL,
                code_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_codes_user
            ON email_codes(user_id, purpose)
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

        # Phase R2: foreign_keys=ON ломает групповые сообщения, если recipient_id
        # ссылается на users(id) — в группах это служебный sentinel 0.
        _migrate_messages_recipient_fk(conn)

        # Phase 7.6d-fix: канал объявлений без бота в users
        _migrate_channel_columns(conn)   # peer_type/peer_id для существующих сообщений
        _remove_system_user(conn)        # служебный @start удалён вместе с историей
        _ensure_creator(conn)            # creator = первый живой пользователь
        _seed_channel_settings(conn)     # профиль канала: имя/био/аватар/баннер

        conn.commit()


# Phase R2: актуальное определение messages (без FK на recipient_id — см. выше)
MESSAGES_COLUMNS = [
    "id", "sender_id", "recipient_id", "text", "created_at",
    "deleted_for_sender", "deleted_for_recipient", "group_id", "reply_to_id", "edited_at",
]


def _migrate_messages_recipient_fk(conn) -> bool:
    """
    Убрать FOREIGN KEY recipient_id -> users(id) из messages.

    Групповые сообщения пишутся с recipient_id = 0 (sentinel «не адресат-пользователь»),
    поэтому при foreign_keys=ON такойINSERT стал бы ошибкой. Пересобираем таблицу
    один раз; данные и id сохраняются.
    """
    fks = conn.execute("PRAGMA foreign_key_list(messages)").fetchall()
    if not any(fk[2] == "users" and fk[3] == "recipient_id" for fk in fks):
        return False

    conn.commit()  # PRAGMA foreign_keys нельзя менять внутри транзакции
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("""
            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_for_sender INTEGER DEFAULT 0,
                deleted_for_recipient INTEGER DEFAULT 0,
                group_id INTEGER NULL,
                reply_to_id INTEGER NULL,
                edited_at TIMESTAMP NULL,
                FOREIGN KEY (sender_id) REFERENCES users(id)
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        columns = [c for c in MESSAGES_COLUMNS if c in existing]
        conn.execute(
            f"INSERT INTO messages_new ({', '.join(columns)}) SELECT {', '.join(columns)} FROM messages"
        )
        conn.execute("DROP TABLE messages")
        conn.execute("ALTER TABLE messages_new RENAME TO messages")
        conn.commit()
    except Exception:
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    app_logger.info("миграция messages: recipient_id больше не FK (группы используют 0)")
    return True


# ========== Phase R2: броня SQLite ==========

PART_MAX_AGE_SECONDS = 3600  # брошенные .part старше часа удаляются при старте

# Служебное соединение, удерживающее WAL открытым всё время работы процесса
_KEEPALIVE_CONN = None


def cleanup_stale_parts(directory: str, max_age: float = PART_MAX_AGE_SECONDS) -> int:
    """Удалить *.part (недописанные вложения) старше max_age секунд."""
    removed = 0
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".part"):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path) or now - os.path.getmtime(path) <= max_age:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def db_integrity_check(conn) -> tuple[bool, str]:
    """PRAGMA integrity_check → (ok, текст результата)."""
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return False, f"ошибка проверки: {exc}"
    messages = [str(row[0]) for row in rows]
    ok = messages == ["ok"]
    return ok, "; ".join(messages)[:500]


def init_db() -> None:
    """
    Стартовая инициализация: схема, персистентный WAL, проверка целостности,
    уборка брошенных .part.
    """
    global _KEEPALIVE_CONN
    app_logger.info("старт приложения: DATA_DIR=%s, PORT=%s", DATA_DIR, PORT)
    ensure_schema()
    with get_db() as conn:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        app_logger.info("journal_mode = %s", mode)
        ok, detail = db_integrity_check(conn)
        app_logger.info("integrity_check %s", detail)
        if not ok:
            error_logger.error("integrity_check провален: %s", detail)
        LAST_INTEGRITY.update(
            ok=ok, detail=detail, ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        _pulse_emit("integrity", f"{'ok' if ok else 'FAIL'}: {detail}")

    # Держим одно служебное соединение: пока оно открыто, SQLite не удаляет
    # messenger.db-wal/-shm после каждого запроса (иначе WAL-файлы живут ровно
    # до закрытия последней коннекции) и сам ведёт контрольные точки.
    _KEEPALIVE_CONN = get_conn()
    _KEEPALIVE_CONN.execute("SELECT 1")

    removed = cleanup_stale_parts(UPLOADS_DIR) + cleanup_stale_parts(THEME_IMAGES_DIR)
    if removed:
        app_logger.info("чистка: удалено брошенных .part: %s", removed)
        _pulse_emit("cleanup", f"part removed={removed}")


init_db()


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
    if not row:
        return None
    user = dict(row)
    # Phase R6 (A07): сессия валидна, только пока её epoch совпадает с epoch пользователя.
    # Смена пароля поднимает epoch в БД — все остальные устройства разлогиниваются.
    if request.session.get("epoch") != user.get("session_epoch", 0):
        return None
    return user


def get_current_user_fresh(request: Request):
    """Get current user with fresh data from DB (for ban check)"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    if request.session.get("epoch") != (row["session_epoch"] if "session_epoch" in row.keys() else 0):
        return None
    return dict(row)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring and load balancers."""
    return JSONResponse({"status": "ok", "version": "5.0"})


# Phase 7.4b: Service Worker — served from root with Service-Worker-Allowed: /
from starlette.responses import Response as StarletteResponse

@app.get("/sw.js")
async def service_worker():
    sw_path = os.path.join(os.path.dirname(__file__), 'static', 'sw.js')
    with open(sw_path, 'r') as f:
        content = f.read()
    return StarletteResponse(content, media_type='application/javascript',
                             headers={'Service-Worker-Allowed': '/'})


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
        # Phase 7.6d-fix: creator — первый зарегистрированный пользователь (ботов в users нет)
        is_creator = 1 if count == 0 else 0
        
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
            "INSERT INTO users (name, username, password_hash, is_admin, email, is_creator) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, username, password_hash, is_admin, email_clean, is_creator)
        )
        new_user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        if invite_code and count > 0:
            conn.execute("UPDATE invites SET used_by = ? WHERE code = ?", (new_user_id, invite_code))
        
        if is_creator:
            app_logger.info("creator назначен: user_id=%s", new_user_id)
        
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
        # в лог — только счётчик и хост: ни email, ни пароль, ни cookie
        _login_logger.warning("логин неудачный: ip=%s, попыток подряд=%s", ip, count)
        if count >= _RATE_LIMIT_MAX:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many login attempts. Try again later."},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)}
            )
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid email or password"})
    
    _clear_failures(ip)
    _pulse_emit("login", f"OK ip={ip} user={user['username']} id={user['id']}")
    _login_logger.info("логин: ip=%s, user_id=%s", ip, user["id"])
    # Phase R6 (A07): логин = НОВАЯ сессия. Старые данные сессии выбрасываем,
    # иначе атакующий может подсунуть заранее известный session id (session fixation).
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["epoch"] = user["session_epoch"]
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
        # Phase 7.6d-fix: канал объявлений — не пользователь, в этом списке его нет
        # (отрисовывается отдельной строкой поверх списка)
        users = conn.execute("""
            SELECT u.id, u.name, u.username, u.avatar_uuid,
                   CASE WHEN p.contact_id IS NULL THEN 0 ELSE 1 END AS pinned
            FROM users u
            LEFT JOIN pins p ON p.contact_id = u.id AND p.user_id = ?
            WHERE u.id != ?
            ORDER BY pinned DESC, u.id ASC
        """, (user["id"], user["id"])).fetchall()

        channel = channel_payload(conn)

    # Ensure theme_json is never None - default to '{}'
    if user.get("theme_json") is None:
        user["theme_json"] = "{}"
    
    return templates.TemplateResponse(request, "chat.html", {
        "user": user,
        "users": [dict(u) for u in users],
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        # Phase 7.6d-fix: канал и флаг creator (инпут/плашка, оформление канала)
        "channel": channel,
        "is_creator": int(user.get("is_creator") or 0),
        # шаблон первого объявления (кнопка «Вставить шаблон» у creator в пустом канале)
        "welcome_template": WELCOME_TEMPLATE,
    })


@app.get("/api/users")
async def api_users(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        users = conn.execute(
            "SELECT id, name, username, avatar_uuid, bio, last_seen FROM users WHERE id != ?",
            (user["id"],),
        ).fetchall()
    
    # Phase 7.8: присутствие — online берём из живых WS-подключений (БД не хранит статус)
    online_ids = set(app.state.connections.keys())
    rows = []
    for u in users:
        item = dict(u)
        item["online"] = int(u["id"]) in online_ids
        rows.append(item)
    return JSONResponse(rows)


@app.get("/api/user/{user_id}/profile")
async def api_user_profile(request: Request, user_id: int):
    """Get another user's profile (name, username, bio, avatar_uuid) - requires auth"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, username, avatar_uuid, bio, banner_uuid, theme_json, last_seen "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    
    payload = dict(row)
    # Phase 7.8: присутствие собеседника (для островка «был(а) в сети …»)
    payload["online"] = int(user_id) in app.state.connections
    return JSONResponse(payload)


@app.get("/api/messages/{recipient_id}")
async def api_messages(request: Request, recipient_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        messages = conn.execute("""
            SELECT m.id, m.sender_id, m.recipient_id, m.text, m.created_at, m.edited_at,
                   sender.avatar_uuid as sender_avatar_uuid,
                   m.peer_type, m.peer_id,
                   m.reply_to_id,
                   r.text AS reply_to_text, ru.name AS reply_to_name
            FROM messages m
            JOIN users sender ON m.sender_id = sender.id
            LEFT JOIN messages r ON r.id = m.reply_to_id
            LEFT JOIN users ru ON ru.id = r.sender_id
            WHERE m.group_id IS NULL
              -- Phase 7.6d-fix: объявления канала в личные диалоги не попадают
              AND IFNULL(m.peer_type, 'user') != 'broadcast'
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


# ============== Phase 7.6d-fix: канал объявлений (без бота в users) ==============

async def push_to_all(payload: dict, exclude_uid: int | None = None) -> None:
    """Разослать событие всем живым WS-подключениям (канал читают все)."""
    for uid, ws_conn in list(app.state.connections.items()):
        if exclude_uid is not None and int(uid) == int(exclude_uid):
            continue
        try:
            await ws_conn.send_json(payload)
        except Exception:
            pass


def channel_message_rows(conn, limit: int | None = None) -> list:
    """История канала: peer_type='broadcast', sender_id — реальный создатель."""
    sql = """
        SELECT m.id, m.sender_id, m.text, m.created_at, m.edited_at,
               u.name AS sender_name, u.username AS sender_username,
               u.avatar_uuid AS sender_avatar_uuid
        FROM messages m
        LEFT JOIN users u ON u.id = m.sender_id
        WHERE m.peer_type = 'broadcast'
        ORDER BY m.created_at ASC, m.id ASC
    """
    rows = conn.execute(sql).fetchall() if limit is None else conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.update({
            "peer_type": CHANNEL_PEER_TYPE,
            "peer_id": 0,
            "is_broadcast": 1,
            "channel": 1,
            "group_id": None,
            "recipient_id": 0,
            "sender_name": item.get("sender_name") or CHANNEL_NAME_DEFAULT,
            "sender_username": item.get("sender_username") or "",
            "sender_avatar_uuid": item.get("sender_avatar_uuid") or "",
            "attachments": [],
            "reply_to_id": None,
        })
        result.append(item)
    return result


@app.get("/api/channel")
async def get_channel(request: Request):
    """Профиль канала: имя, био, аватар, баннер + права вызывающего."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db() as conn:
        payload = channel_payload(conn)
        payload["is_creator"] = int(is_creator_user(user))
        payload["creator_id"] = creator_user_id(conn)
    return JSONResponse(payload)


@app.post("/api/channel")
async def update_channel(request: Request, name: str = Form(""), bio: str = Form("")):
    """Оформить канал: имя и описание. Только creator (админы — читатели)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    require_creator(user)

    clean_name = (name or "").strip()[:50]
    if not clean_name:
        raise HTTPException(status_code=400, detail="Название канала обязательно")
    clean_bio = (bio or "").strip()[:200]

    with get_db() as conn:
        set_channel_fields(conn, {"broadcast_name": clean_name, "broadcast_bio": clean_bio})
        conn.commit()
        payload = channel_payload(conn)
    log_admin_action(user["id"], "channel_profile", None, f"name={clean_name}")
    payload["is_creator"] = 1
    app_logger.info("канал оформлен: creator_id=%s name=%r", user["id"], clean_name)
    return JSONResponse({"success": True, "channel": payload})


def _save_channel_image(upload: UploadFile, subdir: str) -> tuple[str, str]:
    """Сохранить картинку профиля канала (avatars/ или banners/). Возвращает uuid-имя."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    mime_type = upload.content_type or mimetypes.guess_type(upload.filename)[0]
    if not mime_type or not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")
    ext = Path(upload.filename).suffix.lower()
    uuid_name = f"{uuid.uuid4().hex}{ext}"
    target_dir = os.path.join(UPLOADS_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    return uuid_name, os.path.join(target_dir, uuid_name)


@app.post("/api/channel/avatar")
async def upload_channel_avatar(request: Request, avatar: UploadFile = File(...)):
    """Аватар канала — только creator."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    require_creator(user)
    uuid_name, file_path = _save_channel_image(avatar, "avatars")
    part_path = file_path + ".part"
    data = await avatar.read()
    try:
        async with aiofiles.open(part_path, "wb") as f:
            await f.write(data)
        os.replace(part_path, file_path)
    except Exception:
        try:
            await aiofiles.os.remove(part_path)
        except OSError:
            pass
        raise
    with get_db() as conn:
        set_channel_fields(conn, {"broadcast_avatar_uuid": uuid_name})
        conn.commit()
    return JSONResponse({"avatar_uuid": uuid_name})


@app.post("/api/channel/banner")
async def upload_channel_banner(request: Request, banner: UploadFile = File(...)):
    """Баннер канала — только creator."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    require_creator(user)
    uuid_name, file_path = _save_channel_image(banner, "banners")
    part_path = file_path + ".part"
    data = await banner.read()
    try:
        async with aiofiles.open(part_path, "wb") as f:
            await f.write(data)
        os.replace(part_path, file_path)
    except Exception:
        try:
            await aiofiles.os.remove(part_path)
        except OSError:
            pass
        raise
    with get_db() as conn:
        set_channel_fields(conn, {"broadcast_banner_uuid": uuid_name})
        conn.commit()
    return JSONResponse({"banner_uuid": uuid_name})


@app.get("/api/channel/messages")
async def get_channel_messages(request: Request):
    """История канала объявлений: одна лента на всех, пишет в неё только creator."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db() as conn:
        messages = channel_message_rows(conn)
        payload = channel_payload(conn)
        payload["is_creator"] = int(is_creator_user(user))
        payload["creator_id"] = creator_user_id(conn)
    return JSONResponse({
        "messages": messages,
        "channel": payload,
        "is_creator": payload["is_creator"],
    })


async def post_channel_message(request: Request, user, text: str) -> JSONResponse:
    """
    Объявление в канал: одна строка на всех (peer_type='broadcast', peer_id=0),
    автор — реальный создатель. Доставка — веером по WS.
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    if len(text) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Message too long")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, text, group_id, reply_to_id, "
            "peer_type, peer_id, created_at) VALUES (?, 0, ?, NULL, NULL, ?, 0, ?)",
            (user["id"], text, CHANNEL_PEER_TYPE, now),
        )
        message_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT m.id, m.sender_id, m.text, m.created_at, m.edited_at, "
            "u.name AS sender_name, u.username AS sender_username, u.avatar_uuid AS sender_avatar_uuid "
            "FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        item = dict(row)
        item.update({
            "type": "message",
            "peer_type": CHANNEL_PEER_TYPE,
            "peer_id": 0,
            "is_broadcast": 1,
            "channel": 1,
            "group_id": None,
            "recipient_id": 0,
            "sender_name": item.get("sender_name") or CHANNEL_NAME_DEFAULT,
            "sender_username": item.get("sender_username") or "",
            "sender_avatar_uuid": item.get("sender_avatar_uuid") or "",
            "attachments": [],
            "success": True,
        })

    app_logger.info("объявление в канале: creator_id=%s message_id=%s", user["id"], message_id)
    await push_to_all(item, exclude_uid=user["id"])
    return JSONResponse(item)


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
    # Phase 7.6d-fix: канал присылает type='channel', id=0 — внутри это peer_type='broadcast'
    peer_type = normalize_read_peer_type(body.get("type", "user"))
    peer_id = body.get("id")
    if peer_id is None:
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
              AND IFNULL(peer_type, 'user') != 'broadcast'
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
        # Phase 7.6d-fix: канал объявлений — общая лента, непрочитанное считаем по created_at
        row = conn.execute(
            "SELECT last_read_at FROM reads WHERE user_id = ? AND peer_type = ? AND peer_id = 0",
            (user["id"], CHANNEL_PEER_TYPE),
        ).fetchone()
        last_read = row["last_read_at"] if row else "1970-01-01 00:00:00"
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE peer_type = ? AND sender_id != ? AND created_at > ?",
            (CHANNEL_PEER_TYPE, user["id"], last_read),
        ).fetchone()["c"]
        if count > 0:
            result.append({"type": "channel", "id": 0, "count": count})
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


# ============== Phase 7.8: присутствие (online / last_seen) ==============
#
# online — факт живого WS-подключения (app.state.connections: user_id -> websocket).
# last_seen — метка в users: пишется на подключении, на отключении и раз в минуту
# для всех, кто держит соединение (heartbeat). Клиенты получают событие presence.

PRESENCE_HEARTBEAT_SECONDS = 60
_presence_task = None


def touch_last_seen(user_id: int) -> str:
    """Обновить last_seen пользователя, вернуть новую метку (YYYY-MM-DD HH:MM:SS)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id))
            conn.commit()
    except Exception:
        app_logger.exception("presence: не обновился last_seen user_id=%s", user_id)
    return now


async def push_presence(user_id: int, online: bool, last_seen: str | None = None) -> None:
    """Разослать событие присутствия всем, кроме самого пользователя."""
    await push_to_all({
        "type": "presence",
        "user_id": int(user_id),
        "online": bool(online),
        "last_seen": last_seen or "",
    }, exclude_uid=user_id)


async def presence_heartbeat() -> None:
    """Пока пользователь онлайн, его last_seen должен идти вперёд (не только на выход)."""
    while True:
        await asyncio.sleep(PRESENCE_HEARTBEAT_SECONDS)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as conn:
                for uid in list(app.state.connections.keys()):
                    conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, uid))
                conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            app_logger.exception("presence: сбой heartbeat")


def ensure_presence_heartbeat() -> None:
    """Поднять фоновую задачу один раз, когда появилось первое соединение."""
    global _presence_task
    if _presence_task is not None and not _presence_task.done():
        return
    try:
        _presence_task = asyncio.create_task(presence_heartbeat())
    except RuntimeError:      # нет событийного цикла (импорт/тесты) — просто не страхуемся
        _presence_task = None


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
    # Phase 7.8: присутствие — вошёл в сеть, метку тоже обновляем
    ensure_presence_heartbeat()
    _last_seen = touch_last_seen(user_id)
    await push_presence(user_id, True, _last_seen)
    _pulse_emit("ws", f"CONNECT user_id={user_id}")
    _ws_logger.info("WS подключён: user_id=%s", user_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            # Phase 7.3: typing indicator — relay to participants
            if msg_type == "typing":
                group_id = data.get("group_id")
                with get_db() as conn:
                    sender_row = conn.execute("SELECT name, username FROM users WHERE id = ?", (user_id,)).fetchone()
                sender_name = sender_row["name"] if sender_row else "User"
                sender_username = sender_row["username"] if sender_row else ""
                payload = {"type": "typing", "user_id": user_id, "name": sender_name, "username": sender_username}
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
                    # Phase 7.6d-fix: «канал печатает» — бессмыслица, канал не собеседник
                    if data.get("channel"):
                        continue
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
        _ws_logger.info("WS отключён: user_id=%s", user_id)
        if user_id in app.state.connections:
            del app.state.connections[user_id]
        # Phase 7.8: вышел из сети — пишем last_seen и рассылаем событие
        await push_presence(user_id, False, touch_last_seen(user_id))
    except Exception as exc:
        _pulse_emit("ws", f"DISCONNECT user_id={user_id} (error)")
        _ws_logger.warning("WS отключён с ошибкой: user_id=%s: %s", user_id, exc)
        if user_id in app.state.connections:
            del app.state.connections[user_id]
        await push_presence(user_id, False, touch_last_seen(user_id))


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
    final_path = os.path.join(THEME_IMAGES_DIR, uuid_name)
    # Phase R2: *.part + os.replace — клиент не видит недописанный файл
    part_path = final_path + ".part"

    # Save file in chunks with size limit
    total_bytes = 0
    try:
        async with aiofiles.open(part_path, 'wb') as out_file:
            while True:
                chunk = await image.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > THEME_IMAGE_MAX_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Image exceeds size limit ({THEME_IMAGE_MAX_BYTES // 1024} KB)"
                    )
                await out_file.write(chunk)
        os.replace(part_path, final_path)
    except Exception:
        try:
            await aiofiles.os.remove(part_path)
        except OSError:
            pass
        raise

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
    channel: int = Form(0),
    files: list[UploadFile] = File(default=[])
):
    user = get_current_user_fresh(request)  # Use fresh data to catch ban status
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Phase 7.6d-fix: канал объявлений — вещание только для creator, остальным 403
    # (проверяем ДО всех остальных веток: админы тут не имеют привилегий)
    if channel:
        require_creator(user)
        if files:
            raise HTTPException(status_code=400, detail="Канал объявлений принимает только текст")
        return await post_channel_message(request, user, text)
    
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
        
        # Phase 7.6d-fix: peer_type/peer_id — явная маркировка получателя (канал = 0)
        conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, text, group_id, reply_to_id, "
            "peer_type, peer_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], recipient_id, text, group_id if is_group else None,
             reply_target["id"] if reply_target else None,
             PEER_TYPE_GROUP if is_group else PEER_TYPE_USER,
             group_id if is_group else recipient_id)
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
                final_path = os.path.join(UPLOADS_DIR, uuid_name)
                # Phase R2: пишем в *.part; финальное имя появляется только целиком
                part_path = final_path + ".part"
                
                # Stream file to disk in 64KB chunks while counting bytes
                total_bytes = 0
                try:
                    async with aiofiles.open(part_path, 'wb') as f:
                        while True:
                            chunk_start = time.monotonic()
                            chunk = await file.read(chunk_bytes)
                            if not chunk:
                                break
                            total_bytes += len(chunk)
                            if total_bytes > effective_max_bytes:
                                raise HTTPException(status_code=413, detail=f"File '{file.filename}' exceeds {max_upload_mb} MB limit")
                            await f.write(chunk)
                            # Phase 7.1b-fix: token-bucket – sleep = budget − elapsed
                            if chunk_budget > 0:
                                elapsed = time.monotonic() - chunk_start
                                sleep_time = chunk_budget - elapsed
                                if sleep_time > 0:
                                    await asyncio.sleep(sleep_time)
                    # Phase R2: os.replace — атомарное появление готового файла,
                    # читатель никогда не видит полузаписанное вложение
                    os.replace(part_path, final_path)
                except Exception:
                    # Лимит, обрыв соединения, ошибка диска — полуфайла не остаётся
                    try:
                        await aiofiles.os.remove(part_path)
                    except OSError:
                        pass
                    raise
                
                # Detect mime type from extension
                mime_type = mimetypes.guess_type(file.filename)[0] or file.content_type or "application/octet-stream"
                
                # Store attachment in DB
                conn.execute(
                    "INSERT INTO attachments (message_id, uuid_name, orig_name, mime, size) VALUES (?, ?, ?, ?, ?)",
                    (message_id, uuid_name, file.filename, mime_type, total_bytes)
                )
                attachment_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                _pulse_emit("upload", f"file={file.filename} size={total_bytes} mime={mime_type}")
                _upload_logger.info(
                    "вложение сохранено: message_id=%s file=%s size=%s mime=%s",
                    message_id, file.filename, total_bytes, mime_type
                )
        
        conn.commit()
        
        # Get the inserted message with attachments
        msg = conn.execute(
            "SELECT id, sender_id, recipient_id, group_id, text, created_at, reply_to_id, "
            "peer_type, peer_id FROM messages WHERE id = ?",
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
    admin_logger.info(
        "админ-действие: actor_id=%s action=%s target_id=%s detail=%s",
        actor_id, action, target_id, detail
    )


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


@app.post("/api/admin/cleanup-orphans")
async def cleanup_orphan_files(request: Request):
    """
    Phase R2: удалить файлы-сироты из uploads — те, на которые нет строки в attachments.
    Аватары/баннеры лежат в подкаталогах и не трогаются; *.part не трогаются
    (их убирает стартовая чистка по возрасту).
    """
    user = get_current_user(request)
    require_admin(user)

    with get_db() as conn:
        known = {row[0] for row in conn.execute("SELECT uuid_name FROM attachments").fetchall()}

    deleted, errors = 0, 0
    try:
        names = os.listdir(UPLOADS_DIR)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось прочитать uploads: {exc}")

    for name in names:
        path = os.path.join(UPLOADS_DIR, name)
        if not os.path.isfile(path):          # подкаталоги avatars/ и banners/ пропускаем
            continue
        if name.endswith(".part") or name in known:
            continue
        try:
            os.remove(path)
            deleted += 1
        except OSError:
            errors += 1

    log_admin_action(user["id"], "cleanup_orphans", None, f"deleted={deleted}; errors={errors}")
    _pulse_emit("cleanup", f"orphans deleted={deleted}")
    return JSONResponse({"deleted": deleted, "errors": errors, "kept": len(known)})


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



# ===================================================================== #
# Phase R7: подтверждение действий кодами из письма
# ===================================================================== #

SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = (os.environ.get("SMTP_USER") or "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS") or ""
SMTP_FROM = (os.environ.get("SMTP_FROM") or "").strip() or SMTP_USER or "noreply@localhost"
SMTP_STARTTLS = (os.environ.get("SMTP_STARTTLS") or "yes").strip().lower() in ("1", "yes", "true", "on")
SMTP_ENABLED = bool(SMTP_HOST)
# Phase R7: без SMTP_host письма не уходят — в prod это 503, в dev коды видны в app.log
MAIL_BACKEND = "smtp" if SMTP_ENABLED else ("console" if APP_MODE != "prod" else "none")

CODE_TTL_SECONDS = 600        # TTL кода — 10 минут
CODE_MAX_ATTEMPTS = 5         # попыток ввода на один код
CODE_RESEND_INTERVAL = 60     # не чаще одного кода в минуту (на юзера + цель)
CODE_MAX_PER_HOUR = 5         # и не больше пяти в час на юзера
CODE_LENGTH = 6
CODE_PURPOSES = ("verify", "email_change", "password_change")


class EmailUnavailable(Exception):
    """Почта не настроена: в prod это 503, в dev коды уходят в лог."""


def _generate_code() -> str:
    """6 цифр из криптографического ГСЧ (никаких random.randint)."""
    return "".join(str(secrets.randbelow(10)) for _ in range(CODE_LENGTH))


def _code_hash(code: str) -> str:
    """Храним только sha256: plaintext кода в БД не живёт ни секунды."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def _send_code_email(to_address: str, purpose: str, code: str, user_id: int) -> None:
    """
    Отправить код. Реальный SMTP, если задан SMTP_HOST; иначе в dev — console-бэкенд
    (код виден в app.log), а в prod — 503 «почта не настроена».
    """
    if SMTP_ENABLED:
        subject = "Код подтверждения VibeBunker"
        body = (
            f"Код подтверждения: {code}\n\n"
            f"Действует {CODE_TTL_SECONDS // 60} минут, попыток ввода: {CODE_MAX_ATTEMPTS}.\n"
            "Если вы это не запрашивали — просто проигнорируйте письмо."
        )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = SMTP_FROM
        message["To"] = to_address
        message.set_content(body)
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                if SMTP_STARTTLS:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.send_message(message)
        except Exception as exc:
            # в лог — только факт и тип сбоя: ни пароля, ни кода
            error_logger.error("почта: не удалось отправить код (%s): %s", purpose, type(exc).__name__)
            raise HTTPException(status_code=502, detail="Не удалось отправить письмо") from exc
        app_logger.info("код отправлен: purpose=%s user_id=%s (по адресу из профиля)", purpose, user_id)
        return

    if APP_MODE == "prod":
        raise EmailUnavailable()

    # console-бэкенд (только dev)
    app_logger.info("EMAIL CODE user_id=%s purpose=%s code=%s", user_id, purpose, code)


def _code_row(user_id: int, purpose: str, conn) -> sqlite3.Row | None:
    """Актуальная (не использованная, не истёкшая) запись кода."""
    return conn.execute(
        """
        SELECT * FROM email_codes
        WHERE user_id = ? AND purpose = ? AND used = 0
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, purpose),
    ).fetchone()


def _check_code_rate_limit(user_id: int, purpose: str) -> None:
    """1 код в 60 секунд и не больше CODE_MAX_PER_HOUR в час на пользователя."""
    now = datetime.now()
    with get_db() as conn:
        last = conn.execute(
            """
            SELECT created_at FROM email_codes
            WHERE user_id = ? AND purpose = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, purpose),
        ).fetchone()
        if last:
            try:
                created = datetime.fromisoformat(str(last["created_at"]).replace("Z", ""))
            except ValueError:
                created = None
            if created:
                elapsed = (now - created).total_seconds()
                if elapsed < CODE_RESEND_INTERVAL:
                    app_logger.warning(
                        "код: слишком частая отправка (purpose=%s user_id=%s, %.0fс < %dс)",
                        purpose, user_id, elapsed, CODE_RESEND_INTERVAL,
                    )
                    wait = int(CODE_RESEND_INTERVAL - elapsed)
                    raise HTTPException(
                        status_code=429,
                        detail=f"Следующий код можно запросить через {wait} с",
                        headers={"Retry-After": str(wait)},
                    )
        hour_ago = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM email_codes WHERE user_id = ? AND created_at > ?",
            (user_id, hour_ago),
        ).fetchone()["n"]
        if count >= CODE_MAX_PER_HOUR:
            app_logger.warning("код: лимит отправок в час (user_id=%s, %s шт.)", user_id, count)
            raise HTTPException(status_code=429, detail="Слишком много кодов за час, попробуйте позже")


def issue_email_code(user_id: int, purpose: str, to_address: str, target: str | None = None) -> None:
    """Выпустить код, инвалидировать предыдущие и отправить письмо."""
    if purpose not in CODE_PURPOSES:
        raise HTTPException(status_code=400, detail="Неизвестная цель кода")
    _check_code_rate_limit(user_id, purpose)
    code = _generate_code()
    now = datetime.now()
    with get_db() as conn:
        # все прошлые коды по этой цели сразу гасим: валиден ровно один, последний
        conn.execute(
            "UPDATE email_codes SET used = 1 WHERE user_id = ? AND purpose = ? AND used = 0",
            (user_id, purpose),
        )
        conn.execute(
            """
            INSERT INTO email_codes (user_id, purpose, target, code_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, purpose, target, _code_hash(code),
                now.strftime("%Y-%m-%d %H:%M:%S"),
                (now + timedelta(seconds=CODE_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    _send_code_email(to_address, purpose, code, user_id)


def consume_email_code(user_id: int, purpose: str, code: str):
    """
    Проверить код. Возвращает запись (нужен target) либо кидает HTTPException.
    Счётчик попыток растёт, после CODE_MAX_ATTEMPTS код блокируется.
    """
    with get_db() as conn:
        row = _code_row(user_id, purpose, conn)
        if not row:
            raise HTTPException(status_code=400, detail="Код не найден или истёк, запросите новый")
        if row["attempts"] >= CODE_MAX_ATTEMPTS:
            conn.execute("UPDATE email_codes SET used = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            raise HTTPException(status_code=429, detail="Слишком много неверных попыток, запросите новый код")
        try:
            expired = datetime.fromisoformat(str(row["expires_at"])) < datetime.now()
        except ValueError:
            expired = True
        if expired:
            conn.execute("UPDATE email_codes SET used = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            raise HTTPException(status_code=400, detail="Срок действия кода истёк, запросите новый")
        if not hmac.compare_digest(str(row["code_hash"]), _code_hash(code)):
            attempts = int(row["attempts"]) + 1
            blocked = attempts >= CODE_MAX_ATTEMPTS
            conn.execute(
                "UPDATE email_codes SET attempts = ?, used = ? WHERE id = ?",
                (attempts, 1 if blocked else 0, row["id"]),
            )
            conn.commit()
            app_logger.warning(
                "код: неверная попытка (purpose=%s user_id=%s, %d/%d%s)",
                purpose, user_id, attempts, CODE_MAX_ATTEMPTS, " — код заблокирован" if blocked else "",
            )
            raise HTTPException(
                status_code=400,
                detail=("Код заблокирован: слишком много попыток, запросите новый"
                        if blocked else "Неверный код"),
            )
        conn.execute("UPDATE email_codes SET used = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        app_logger.info("код принят: purpose=%s user_id=%s", purpose, user_id)
        return dict(row)


# ========== PROFILE ENDPOINTS ==========
@app.get("/api/profile")
async def get_profile(request: Request):
    """Get current user's profile data"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, username, email, email_verified, avatar_uuid, bio, font_scale, banner_uuid, "
            "is_creator FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()

    payload = dict(row)
    # Phase R7: фронту нужны и статус верификации, и доступность почты как таковой
    payload["mail_backend"] = MAIL_BACKEND
    # Phase 7.6d-fix: creator оформляет канал объявлений (профиль канала — в settings)
    payload["is_creator"] = int(user.get("is_creator") or 0)
    return JSONResponse(payload)


@app.post("/api/profile")
async def update_profile(request: Request, name: str = Form(...), bio: str = Form("")):
    """Update current user's profile (name and bio). Профиль канала — POST /api/channel."""
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


@app.post("/api/profile/font")
async def update_font_scale(request: Request, scale: float = Form(...)):
    """Update font scale (0.9–1.4) for accessibility"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if scale not in (0.9, 1.0, 1.1, 1.25, 1.4):
        raise HTTPException(status_code=400, detail="Invalid font scale")
    with get_db() as conn:
        conn.execute("UPDATE users SET font_scale = ? WHERE id = ?", (scale, user["id"]))
        conn.commit()
    return JSONResponse({"success": True, "font_scale": scale})


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
async def change_email(request: Request, email: str = Form(...), code: str = Form(""), password: str = Form("")):
    """
    Phase R7: второй шаг смены почты. Шаг первый — /api/email/code/request
    с purpose=email_change: код уходит на НОВЫЙ адрес.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    email_clean = email.strip().lower()
    if not validate_email_format(email_clean):
        raise HTTPException(status_code=400, detail="Invalid email format")

    if not code.strip():
        raise HTTPException(status_code=400, detail="Требуется код из письма (сначала запросите его)")

    row = consume_email_code(user["id"], "email_change", code)
    target = (row.get("target") or "").strip().lower()
    if target != email_clean:
        raise HTTPException(status_code=400, detail="Код был отправлен на другой адрес")

    with get_db() as conn:
        conn.execute(
            "UPDATE users SET email = ?, email_verified = 1 WHERE id = ?", (email_clean, user["id"])
        )
        conn.commit()

    app_logger.info("смена почты: user_id=%s (код подтверждён)", user["id"])
    return JSONResponse({"success": True, "email": email_clean, "email_verified": 1})


# ---------------- Phase R7: запрос и подтверждение кодов ----------------
@app.post("/api/email/code/request")
async def request_email_code(
    request: Request,
    purpose: str = Form(...),
    new_email: str = Form(""),
    current_password: str = Form(""),
):
    """
    Выпустить 6-значный код и отправить его письмом.
    purpose: verify | email_change | password_change.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    purpose = (purpose or "").strip()
    if purpose not in CODE_PURPOSES:
        raise HTTPException(status_code=400, detail="Неизвестная цель кода")

    if MAIL_BACKEND == "none":
        raise HTTPException(status_code=503, detail="Почта не настроена: задайте SMTP_HOST")

    recipient = (user.get("email") or "").strip()
    target: str | None = None

    if purpose == "password_change":
        if not verify_password(current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        if not recipient:
            raise HTTPException(status_code=400, detail="У аккаунта нет почты — код некуда отправить")
    elif purpose == "email_change":
        if not verify_password(current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        new_email_clean = new_email.strip().lower()
        if not validate_email_format(new_email_clean):
            raise HTTPException(status_code=400, detail="Invalid email format")
        with get_db() as conn:
            taken = conn.execute(
                "SELECT id FROM users WHERE email = ? AND id != ?", (new_email_clean, user["id"])
            ).fetchone()
        if taken:
            raise HTTPException(status_code=400, detail="Email already registered")
        recipient = new_email_clean       # код уходит на НОВУЮ почту
        target = new_email_clean
    else:  # verify
        if not recipient:
            raise HTTPException(status_code=400, detail="У аккаунта нет почты — код некуда отправить")
        if new_email.strip():
            candidate = new_email.strip().lower()
            if not validate_email_format(candidate):
                raise HTTPException(status_code=400, detail="Invalid email format")
            recipient = candidate
            target = candidate

    # SMTP — синхронный: не блокируем event loop
    await run_in_threadpool(issue_email_code, user["id"], purpose, recipient, target)
    return JSONResponse({"success": True, "purpose": purpose, "sent_to": _mask_email(recipient)})


def _mask_email(address: str) -> str:
    """Для ответа клиенту: a****b@domain — сам адрес наружу не светим."""
    if "@" not in address:
        return "***"
    name, domain = address.split("@", 1)
    if len(name) <= 2:
        return f"***@{domain}"
    return f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}@{domain}"


@app.post("/api/email/code/confirm")
async def confirm_email_code(request: Request, purpose: str = Form(...), code: str = Form(...)):
    """
    Подтвердить код: verify → email_verified=1, email_change → почта обновлена и верифицирована.
    (password_change подтверждается в /api/profile/password — там же и меняется пароль.)
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    purpose = (purpose or "").strip()
    if purpose not in ("verify", "email_change"):
        raise HTTPException(status_code=400, detail="Неизвестная цель кода")

    row = consume_email_code(user["id"], purpose, code)

    new_email = (row.get("target") or "").strip().lower() or (user.get("email") or "").strip().lower()
    with get_db() as conn:
        if purpose == "email_change" and new_email:
            taken = conn.execute(
                "SELECT id FROM users WHERE email = ? AND id != ?", (new_email, user["id"])
            ).fetchone()
            if taken:
                raise HTTPException(status_code=400, detail="Email already registered")
            conn.execute(
                "UPDATE users SET email = ?, email_verified = 1 WHERE id = ?", (new_email, user["id"])
            )
        else:
            conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user["id"],))
        conn.commit()
        fresh = conn.execute("SELECT email, email_verified FROM users WHERE id = ?", (user["id"],)).fetchone()

    app_logger.info("почта подтверждена: user_id=%s purpose=%s", user["id"], purpose)
    return JSONResponse({"success": True, "email": fresh["email"], "email_verified": fresh["email_verified"]})


@app.post("/api/profile/password")
async def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...), code: str = Form("")):
    """
    Phase R7: смена пароля в два шага.
    Шаг 1 — POST /api/email/code/request с purpose=password_change и текущим паролем,
    шаг 2 — сюда: код из письма + новый пароль. Плюс session_epoch++ (R6 рвёт чужие сессии).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")

    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    if not code.strip():
        raise HTTPException(status_code=400, detail="Требуется код из письма (сначала запросите его)")
    consume_email_code(user["id"], "password_change", code)

    new_hash = hash_password(new_password)
    with get_db() as conn:
        # Phase R6 (A07): поднимаем epoch — все сессии, кроме текущей, становятся невалидными
        conn.execute(
            "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
            (new_hash, user["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT session_epoch FROM users WHERE id = ?", (user["id"],)).fetchone()
    request.session["epoch"] = row["session_epoch"]
    app_logger.info("смена пароля: user_id=%s, прочие сессии сброшены (epoch=%s)", user["id"], row["session_epoch"])

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
    """Upload avatar for current user (аватар канала — POST /api/channel/avatar)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    target_id = int(user["id"])
    
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
    
    # Save file (Phase R2: *.part + os.replace — полуаватаров не бывает)
    part_path = file_path + ".part"
    file_data = await avatar.read()
    try:
        async with aiofiles.open(part_path, 'wb') as f:
            await f.write(file_data)
        os.replace(part_path, file_path)
    except Exception:
        try:
            await aiofiles.os.remove(part_path)
        except OSError:
            pass
        raise
    
    # Update user record (Phase 7.6d: target_id — свой профиль или канал start для creator)
    with get_db() as conn:
        conn.execute("UPDATE users SET avatar_uuid = ? WHERE id = ?", (uuid_name, target_id))
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


@app.post("/api/profile/banner")
async def upload_banner(request: Request, banner: UploadFile = File(...)):
    """Upload banner for current user (баннер канала — POST /api/channel/banner)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    target_id = int(user["id"])
    if not banner.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    mime_type = banner.content_type or mimetypes.guess_type(banner.filename)[0]
    if not mime_type or not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")
    ext = Path(banner.filename).suffix.lower()
    uuid_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOADS_DIR, "banners", uuid_name)
    os.makedirs(os.path.join(UPLOADS_DIR, "banners"), exist_ok=True)
    # Phase R2: *.part + os.replace
    part_path = file_path + ".part"
    file_data = await banner.read()
    try:
        async with aiofiles.open(part_path, 'wb') as f:
            await f.write(file_data)
        os.replace(part_path, file_path)
    except Exception:
        try:
            await aiofiles.os.remove(part_path)
        except OSError:
            pass
        raise
    with get_db() as conn:
        conn.execute("UPDATE users SET banner_uuid = ? WHERE id = ?", (uuid_name, target_id))
        conn.commit()
    return JSONResponse({"banner_uuid": uuid_name})


@app.get("/api/banner/{banner_uuid}")
async def get_banner(banner_uuid: str):
    """Serve user banner"""
    file_path = os.path.join(UPLOADS_DIR, "banners", banner_uuid)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Banner not found")
    return StreamingResponse(stream_file(file_path), media_type="image/jpeg")


def _purge_user_references(conn, user_id: int) -> None:
    """
    Phase R2: при foreign_keys=ON удалить пользователя можно, только сняв ссылки.
    Порядок: дочерние строки → владение группами → сообщения → сам пользователь.
    Файлы удалённых вложений остаются на диске — их уберёт cleanup-orphans.
    """
    # группы в собственности: передаём старшему участнику, иначе владельца не будет
    owned = [row[0] for row in conn.execute("SELECT id FROM groups WHERE owner_id = ?", (user_id,)).fetchall()]
    for group_id in owned:
        heir = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ? AND user_id != ? "
            "ORDER BY joined_at ASC, user_id ASC LIMIT 1",
            (group_id, user_id),
        ).fetchone()
        conn.execute("UPDATE groups SET owner_id = ? WHERE id = ?", (heir[0] if heir else None, group_id))

    conn.execute(
        "DELETE FROM attachments WHERE message_id IN "
        "(SELECT id FROM messages WHERE sender_id = ? OR recipient_id = ?)",
        (user_id, user_id),
    )
    conn.execute("DELETE FROM messages WHERE sender_id = ? OR recipient_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM theme_presets WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM reads WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM pins WHERE user_id = ? OR contact_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM mutes WHERE user_id = ? OR contact_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM blocks WHERE blocker_id = ? OR blocked_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM warns WHERE user_id = ? OR by_admin_id = ?", (user_id, user_id))
    conn.execute("UPDATE invites SET used_by = NULL WHERE used_by = ?", (user_id,))
    conn.execute("DELETE FROM invites WHERE created_by = ?", (user_id,))


@app.post("/api/delete-account")
async def delete_account(request: Request):
    """Delete current user's account (Phase R2: ссылки снимаются каскадом)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        was_creator = int(user.get("is_creator") or 0)
        _purge_user_references(conn, user["id"])
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        # Phase 7.6d-fix: creator ушёл — канал переходит к первому живому пользователю
        if was_creator:
            heir = conn.execute("SELECT MIN(id) AS id FROM users").fetchone()
            if heir and heir["id"] is not None:
                conn.execute("UPDATE users SET is_creator = 1 WHERE id = ?", (int(heir["id"]),))
                app_logger.info("creator передан: user_id=%s", int(heir["id"]))
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
        msg = conn.execute(
            "SELECT id, sender_id, group_id, recipient_id, peer_type FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg["sender_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only author can edit")
        if not text.strip():
            raise HTTPException(status_code=400, detail="Empty message")
        # Phase 7.6d-fix: объявление канала правит только его автор-создатель
        is_broadcast = (msg["peer_type"] or PEER_TYPE_USER) == CHANNEL_PEER_TYPE
        if is_broadcast:
            require_creator(user)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE messages SET text = ?, edited_at = ? WHERE id = ?", (text, now, message_id))
        conn.commit()

    event = {
        "type": "message_edited", "message_id": message_id,
        "text": text, "edited_at": now,
        "group_id": msg["group_id"], "sender_id": msg["sender_id"],
        "recipient_id": msg["recipient_id"],
    }
    if is_broadcast:
        # канал читают все — правку видят все онлайн
        event["peer_type"] = CHANNEL_PEER_TYPE
        await push_to_all(event)
        return JSONResponse({"ok": True, "edited_at": now})

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
                await ws_conn.send_json(event)
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

        # Phase 7.6d-fix: объявление канала удаляет только creator
        if (msg["peer_type"] or PEER_TYPE_USER) == CHANNEL_PEER_TYPE:
            require_creator(user)
            conn.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()
            app_logger.info("объявление удалено: creator_id=%s message_id=%s", user["id"], message_id)
            await push_to_all({
                "type": "message_deleted", "message_id": message_id,
                "peer_type": CHANNEL_PEER_TYPE, "peer_id": 0,
            }, exclude_uid=user["id"])
            return JSONResponse({"success": True, "deleted": True})

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
async def delete_chat_endpoint(request: Request, recipient_id: int = Form(0), channel: int = Form(0)):
    """Delete entire chat with a user (marks all messages as deleted for current user)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Phase 7.6d-fix: канал объявлений не удаляют — ни через чат, ни через «занавес»
    if channel or not recipient_id:
        raise HTTPException(status_code=403, detail="Канал объявлений удалить нельзя")
    
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


# Phase R6: CSRF-проверка — самый внешний мидлварь (до логирования и обработки ошибок)
app.add_middleware(CSRFMiddleware)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
