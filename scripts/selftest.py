#!/usr/bin/env python3
"""
ФАЗА R1: SELFTEST — ЩИТ ОТ АГЕНТОВ
===================================

End-to-end самопроверка мессенджера на изолированном инстансе.

Что делает:
  * поднимает СВОЙ экземпляр приложения в subprocess
        PORT=8099, DATA_DIR=<mkdtemp>, SECRET_KEY=test, MAX_UPLOAD_BYTES=5242880
    (рабочий сервер на 8000 и прод-данные не задеваются — см. PREFLIGHT);
  * ждёт /health;
  * прогоняет сценарии a)…l), каждый печатает OK/FAIL;
  * убивает сервер, чистит временный DATA_DIR;
  * печатает итог "SELFTEST: N/N OK" и выходит с кодом 0 только при полном зелёном.

Запуск:
    python scripts/selftest.py

Согласно УСТАВУ (README, п. 11) фаза принимается только с зелёным selftest:
последние 20 строк его вывода вкладываются в отчёт о фазе.
"""

from __future__ import annotations

import asyncio
import base64
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import websockets

# --------------------------------------------------------------------------- #
# Константы прогона
# --------------------------------------------------------------------------- #

HOST = "127.0.0.1"
PORT = 8099                       # по ТЗ: тестовый инстанс живёт на 8099
ENV_PORT = 8098                    # второй инстанс — для сценариев r/s (.env и prod-guard)
BASE = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}/ws"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5242880
SECRET_KEY = "test"

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 20.0            # сколько ждём /health
EVENT_TIMEOUT = 5.0               # сколько ждём WS-событие
HTTP_TIMEOUT = 10.0

# 1x1 PNG — минимальный валидный файл для сценария с вложением
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# копии репозитория и временные DATA_DIR для сценариев r/s (чистим в main())
EXTRA_TEMP_DIRS: list = []

USER_A = ("Alice", "alice@selftest.local", "selftest-pass")
USER_B = ("Bob", "bob@selftest.local", "selftest-pass")


# --------------------------------------------------------------------------- #
# HTTP-клиент на stdlib (urllib + cookiejar, редиректы НЕ идем)
# --------------------------------------------------------------------------- #

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """303 от /login и /register = успех; нам нужен сам код, а не тело редиректа."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----selftest" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for item in files:
        # (поле, имя файла, байты[, Content-Type])
        field, filename, content = item[0], item[1], item[2]
        ctype = item[3] if len(item) > 3 else "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            + content + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Client:
    """Один пользователь = одна cookie-jar = одна сессия."""

    def __init__(self, name: str, email: str, password: str):
        self.name = name
        self.email = email
        self.password = password
        self.id: int | None = None
        self.username: str | None = None
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect
        )

    # -- CSRF (Phase R6): double-submit — cookie csrftoken + заголовок X-CSRF-Token -- #
    def csrf_cookie(self) -> str:
        for cookie in self.jar:
            if cookie.name == "csrftoken":
                return cookie.value
        return ""

    def prime_csrf(self):
        """Первый GET — сервер выдаёт cookie csrftoken (так же, как страница в браузере)."""
        if not self.csrf_cookie():
            self.request("/", method="GET")

    # -- транспорт ---------------------------------------------------------- #
    def request(self, path, method="POST", data=None, files=None, csrf=True):
        method = method.upper()
        mutating = method not in ("GET", "HEAD", "OPTIONS")
        if csrf and mutating:
            self.prime_csrf()
        headers = {}
        body = None
        if csrf and mutating:
            headers["X-CSRF-Token"] = self.csrf_cookie()
        if files is not None:
            body, headers["Content-Type"] = _multipart(data or {}, files)
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:      # 303 / 4xx / 5xx — всё это ответы
            return exc.code, exc.read()

    def json(self, path, method="POST", data=None, files=None, csrf=True):
        status, raw = self.request(path, method=method, data=data, files=files, csrf=csrf)
        try:
            payload = json.loads(raw.decode())
        except Exception:
            raise AssertionError(f"{method} {path}: HTTP {status}, тело не JSON: {raw[:160]!r}")
        if status >= 400:
            raise AssertionError(f"{method} {path}: HTTP {status}: {payload}")
        if not isinstance(payload, (dict, list)):
            raise AssertionError(
                f"{method} {path}: HTTP {status}, неожиданный JSON ({type(payload).__name__}): {raw[:200]!r}"
            )
        return payload

    def session_cookie(self) -> str:
        for cookie in self.jar:
            if cookie.name == "session":
                return f"session={cookie.value}"
        raise AssertionError(f"у {self.name} нет session-cookie (логин не прошёл)")


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #

class Report:
    def __init__(self):
        self.total = 0
        self.failed = 0
        self.t0 = time.monotonic()

    async def step(self, key: str, label: str, fn):
        self.total += 1
        started = time.monotonic()
        try:
            result = fn()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
            print(f"OK   {key}) {label} [{time.monotonic() - started:.2f}s]")
            return result
        except Exception as exc:
            self.failed += 1
            print(f"FAIL {key}) {label}: {type(exc).__name__}: {exc}")
            return None


# --------------------------------------------------------------------------- #
# WS-хелперы
# --------------------------------------------------------------------------- #

async def ws_open(client: Client):
    """Открыть WS от имени пользователя и убедиться, что сервер принял соединение."""
    ws = await websockets.connect(WS_URL, additional_headers={"Cookie": client.session_cookie()})
    # ping→pong: рукопожатие прошло, сессия валидна, соединение живое
    await asyncio.wait_for(ws.ping(), timeout=EVENT_TIMEOUT)
    return ws


def last_email_code(data_dir: str, user_id: int | None = None, purpose: str | None = None) -> str:
    """
    Phase R7: console-бэкенд (dev без SMTP) пишет код в app.log строкой
    «EMAIL CODE user_id=N purpose=... code=NNNNNN» — так тест и достаёт его,
    не залезая в БД (там только sha256).
    """
    log_path = Path(data_dir) / "logs" / "app.log"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if log_path.is_file():
            matches = re.findall(
                r"EMAIL CODE user_id=(\d+) purpose=(\S+) code=(\d{6})",
                log_path.read_text(encoding="utf-8", errors="replace"),
            )
            for uid, purp, code in reversed(matches):
                if user_id is not None and int(uid) != user_id:
                    continue
                if purpose is not None and purp != purpose:
                    continue
                return code
        time.sleep(0.1)
    raise AssertionError(f"код из письма не найден в {log_path}")


def dialog_history(client: Client, peer_id: int) -> list:
    """История диалога 1-на-1: эндпоинт отдаёт {"messages": [...], "muted_by_me": bool}."""
    payload = client.json(f"/api/messages/{peer_id}", method="GET")
    messages = payload.get("messages") if isinstance(payload, dict) else None
    assert isinstance(messages, list), f"история диалога {peer_id}: неожиданный ответ {payload!r}"[:300]
    return messages


def _clean_env() -> dict:
    """Окружение без переменных, которые сценарии r/s задают сами (из .env или явно)."""
    env = dict(os.environ)
    for key in ("SECRET_KEY", "DATA_DIR", "APP_MODE", "PORT",
                "MAX_UPLOAD_BYTES", "FIRST_USER_ADMIN", "BACKUP_DIR"):
        env.pop(key, None)
    return env


def _make_repo_copy() -> str:
    """Копия репозитория во временном каталоге — там можно смело писать .env."""
    path = tempfile.mkdtemp(prefix="selftest-repo-")
    shutil.copytree(
        REPO_ROOT, path,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "data", "*.db", "*.pyc", "*.log", "backups"
        ),
        dirs_exist_ok=True,
    )
    EXTRA_TEMP_DIRS.append(path)
    return path


async def expect_event(ws, predicate, label: str, timeout: float = EVENT_TIMEOUT):
    """Ждать событие, удовлетворяющее predicate; посторонние события пропускаем."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"WS-событие «{label}» не пришло за {timeout:.0f}s")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if predicate(event):
            return event


# --------------------------------------------------------------------------- #
# Жизненный цикл сервера
# --------------------------------------------------------------------------- #

def port_is_busy(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def start_server(data_dir: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        PORT=str(PORT),
        DATA_DIR=data_dir,
        SECRET_KEY=SECRET_KEY,
        MAX_UPLOAD_BYTES=str(MAX_UPLOAD_BYTES),
        PYTHONUNBUFFERED="1",
        # тестовый инстанс всегда по HTTP и без iframe: фиксируем cookie-режим,
        # чтобы прогон не зависел от env разработчика
        SESSION_SAME_SITE="lax",
        SESSION_SECURE="0",
        FRAME_OPTIONS="deny",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", HOST, "--port", str(PORT), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log = (proc.stdout.read() or "") if proc.stdout else ""
            raise RuntimeError(f"сервер упал при старте (код {proc.returncode}):\n{log[-2000:]}")
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return proc
        except urllib.error.HTTPError:
            return proc
        except Exception:
            time.sleep(0.15)
    stop_server(proc)
    raise RuntimeError(f"сервер не поднялся за {STARTUP_TIMEOUT:.0f}s")


def stop_server(proc: subprocess.Popen | None) -> str:
    if proc is None:
        return ""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log = ""
    if proc.stdout:
        try:
            log = proc.stdout.read() or ""
        except Exception:
            log = ""
    try:
        proc.stdout.close()
    except Exception:
        pass
    return log


# --------------------------------------------------------------------------- #
# Сценарии
# --------------------------------------------------------------------------- #

async def run_scenarios(report: Report, data_dir: str) -> None:
    alice = Client(*USER_A)
    bob = Client(*USER_B)
    state: dict = {
        "alice": alice,
        "bob": bob,
        "data_dir": data_dir,
        "db_path": os.path.join(data_dir, "messenger.db"),
        "uploads_dir": os.path.join(data_dir, "uploads"),
    }

    # ---------------- a) /health ----------------
    async def scenario_a():
        with urllib.request.urlopen(f"{BASE}/health", timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
        assert resp.status == 200, f"ожидали 200, получили {resp.status}"
        assert body.get("status") == "ok", f"тело /health: {body}"
    await report.step("a", "/health отвечает 200", scenario_a)

    # ---------------- b) регистрация A (первый → админ) ----------------
    def scenario_b():
        status, raw = alice.request("/register", data={
            "name": alice.name, "email": alice.email,
            "password": alice.password, "invite_code": "",
        })
        assert status == 303, f"регистрация A: HTTP {status} (ожидали 303): {raw[:160]!r}"
        # регистрация сессию не открывает — логинимся и проверяем роль
        status, raw = alice.request("/login", data={
            "username": alice.email, "password": alice.password,
        })
        assert status == 303, f"логин A: HTTP {status}: {raw[:160]!r}"
        # первый пользователь — админ: панель доступна, иначе require_admin даст 401/403
        status, _ = alice.request("/admin", method="GET")
        assert status == 200, f"/admin для A: HTTP {status} (первый юзер не админ?)"
    await report.step("b", "регистрация A, первый пользователь — админ", scenario_b)

    # ---------------- c) A создаёт инвайт, B регистрируется по нему ----------------
    def scenario_c():
        invite = alice.json("/admin/invite")
        code = invite.get("invite_code")
        assert code, f"инвайт не создан: {invite}"
        status, raw = bob.request("/register", data={
            "name": bob.name, "email": bob.email,
            "password": bob.password, "invite_code": code,
        })
        assert status == 303, f"регистрация B по инвайту: HTTP {status}: {raw[:160]!r}"
        state["invite_code"] = code
    await report.step("c", "A создаёт инвайт, B регистрируется по нему", scenario_c)

    # ---------------- d) логины A и B (cookiejar) ----------------
    def scenario_d():
        for client in (alice, bob):
            status, raw = client.request("/login", data={
                "username": client.email, "password": client.password,
            })
            assert status == 303, f"логин {client.name}: HTTP {status}: {raw[:160]!r}"
            assert client.session_cookie(), f"у {client.name} нет session-cookie"
            profile = client.json("/api/profile", method="GET")
            client.id = profile["id"]
            client.username = profile["username"]
        assert alice.id != bob.id, "A и B получили одинаковый id"
    await report.step("d", "логины A и B, сессионные cookie получены", scenario_d)
    state["a_id"], state["b_id"] = alice.id, bob.id

    def need(key: str, hint: str):
        value = state.get(key)
        if value is None:
            raise AssertionError(f"предусловие не выполнено: {hint} (см. FAIL выше)")
        return value

    # ---------------- e) WS-коннекты A и B ----------------
    async def scenario_e():
        state["ws_a"] = await ws_open(alice)
        state["ws_b"] = await ws_open(bob)
    await report.step("e", "WebSocket-подключения A и B установлены", scenario_e)

    # ---------------- f) A→B: WS-событие + история ----------------
    async def scenario_f():
        ws_b = need("ws_b", "нет WS-соединения B")
        a_id, b_id = need("a_id", "нет id A"), need("b_id", "нет id B")
        sent = alice.json("/api/send", data={
            "recipient_id": b_id, "group_id": 0, "text": "привет от A", "reply_to_id": 0,
        })
        msg_id = sent["id"]
        state["msg_ab"] = msg_id

        event = await expect_event(
            ws_b,
            lambda ev: ev.get("id") == msg_id and ev.get("sender_id") == a_id,
            "доставка сообщения A→B",
        )
        assert event.get("text") == "привет от A", f"текст в WS: {event}"

        history = dialog_history(bob, a_id)
        assert any(m["id"] == msg_id and m["text"] == "привет от A" for m in history), \
            f"история B не содержит сообщение {msg_id}: {history}"
    await report.step("f", "A→B сообщение: WS-событие у B и запись в истории", scenario_f)

    # ---------------- g) A→B вложение: байты равны ----------------
    async def scenario_g():
        a_id, b_id = need("a_id", "нет id A"), need("b_id", "нет id B")
        sent = alice.json(
            "/api/send",
            data={"recipient_id": b_id, "group_id": 0, "text": "", "reply_to_id": 0},
            files=[("files", "pixel.png", PNG_1X1)],
        )
        attachments = sent.get("attachments") or []
        assert attachments, f"в ответе /api/send нет вложений: {sent}"
        att_id = attachments[0]["id"]

        status, raw = bob.request(f"/api/attachment/{att_id}", method="GET")
        assert status == 200, f"скачивание вложения: HTTP {status}: {raw[:160]!r}"
        assert raw == PNG_1X1, f"байты не совпали: получили {len(raw)} байт, отправляли {len(PNG_1X1)}"

        history = dialog_history(bob, a_id)
        att_ids = [a["id"] for m in history for a in (m.get("attachments") or [])]
        assert att_id in att_ids, f"вложение {att_id} не видно в истории B: {att_ids}"
        state["att_id"] = att_id
        state["att_name"] = attachments[0]["uuid_name"]
    await report.step("g", "A→B вложение (PNG): B скачивает, байты идентичны", scenario_g)

    # ---------------- h) B отвечает с reply_to_id: у A цитата ----------------
    async def scenario_h():
        a_id, b_id = need("a_id", "нет id A"), need("b_id", "нет id B")
        msg_ab = need("msg_ab", "нет сообщения из сценария f")
        sent = bob.json("/api/send", data={
            "recipient_id": a_id, "group_id": 0, "text": "ответ от B", "reply_to_id": msg_ab,
        })
        reply_id = sent["id"]
        assert sent.get("reply_to_id") == msg_ab, f"reply_to_id не проставлен: {sent}"

        history = dialog_history(alice, b_id)
        quoted = next((m for m in history if m["id"] == reply_id), None)
        assert quoted, f"ответа {reply_id} нет в истории A: {history}"
        assert quoted.get("reply_to_id") == msg_ab, f"reply_to_id в истории: {quoted}"
        assert (quoted.get("reply_to_text") or "").startswith("привет от A"), \
            f"цитата не подтянулась: {quoted.get('reply_to_text')!r}"
    await report.step("h", "B отвечает с reply_to_id: у A в истории цитата", scenario_h)

    # ---------------- i) группа, групповое сообщение по WS, /rename ----------------
    async def scenario_i():
        ws_b = need("ws_b", "нет WS-соединения B")
        b_id = need("b_id", "нет id B")
        group = alice.json("/api/groups", data={"name": "SelftestGroup"})
        group_id = group["id"]
        state["group_id"] = group_id

        added = alice.json(f"/api/groups/{group_id}/members", data={"user_id": b_id})
        assert added.get("success"), f"добавление B в группу: {added}"

        sent = alice.json("/api/send", data={
            "recipient_id": 0, "group_id": group_id, "text": "всем привет", "reply_to_id": 0,
        })
        group_msg_id = sent["id"]

        event = await expect_event(
            ws_b,
            lambda ev: ev.get("id") == group_msg_id and ev.get("group_id") == group_id,
            "групповое сообщение",
        )
        assert event.get("text") == "всем привет", f"текст группового сообщения: {event}"

        renamed = alice.json(f"/api/groups/{group_id}/command",
                             data={"cmd": "rename", "args": "RenamedGroup"})
        assert renamed.get("ok"), f"/rename: {renamed}"

        groups = alice.json("/api/groups", method="GET")
        names = [g["name"] for g in groups]
        assert "RenamedGroup" in names, f"новое имя не видно в /api/groups: {names}"
    await report.step("i", "группа A+B: WS-доставка и команда /rename", scenario_i)

    # ---------------- j) правка своего сообщения и удаление «у всех» ----------------
    async def scenario_j():
        a_id = need("a_id", "нет id A")
        msg_ab = need("msg_ab", "нет сообщения из сценария f")
        edited = alice.json(f"/api/messages/{msg_ab}/edit", data={"text": "привет от A (правка)"})
        assert edited.get("ok"), f"правка сообщения: {edited}"
        assert edited.get("edited_at"), f"edited_at не вернулся: {edited}"

        history = dialog_history(bob, a_id)
        target = next((m for m in history if m["id"] == msg_ab), None)
        assert target, f"сообщения {msg_ab} нет в истории B: {history}"
        assert target.get("edited_at"), f"edited_at не виден в истории: {target}"
        assert target.get("text") == "привет от A (правка)", f"текст после правки: {target}"

        deleted = alice.json("/api/delete-message", data={"message_id": msg_ab, "mode": "all"})
        assert deleted.get("success"), f"удаление «у всех»: {deleted}"

        history = dialog_history(bob, a_id)
        assert not any(m["id"] == msg_ab for m in history), \
            f"сообщение {msg_ab} осталось в истории B после удаления «у всех»"
    await report.step("j", "A правит сообщение (edited_at) и удаляет «у всех»", scenario_j)

    # ---------------- k) тема: POST сохраняет токен, GET возвращает ----------------
    def scenario_k():
        accent = "#ff00aa"
        theme_json = json.dumps({
            "colors": {"accent": accent, "bg": "#101010"},
            "images": {},
            "effects": {},
            "sizing": {},
        })
        saved = alice.json("/api/theme", data={"theme_json": theme_json})
        assert saved.get("success"), f"сохранение темы: {saved}"

        theme = alice.json("/api/theme", method="GET")
        colors = theme.get("colors") or {}
        assert colors.get("accent") == accent, f"токен accent не сохранился: {colors.get('accent')!r}"
        assert colors.get("bg") == "#101010", f"токен bg не сохранился: {colors.get('bg')!r}"
        # незатронутые токены подтянулись из дефолтов
        assert colors.get("text"), f"дефолтные токены потеряны: {colors}"
    await report.step("k", "тема: токен сохраняется POST и возвращается GET", scenario_k)

    # ---------------- l) админ-лог содержит записи о действиях ----------------
    def scenario_l():
        b_id = need("b_id", "нет id B")
        warned = alice.json("/admin/warn", data={"target_user_id": b_id, "reason": "selftest"})
        assert warned.get("warn_count", 0) >= 1, f"варн не выдан: {warned}"

        audit = alice.json("/api/admin/audit", method="GET")
        assert isinstance(audit, list) and audit, f"админ-лог пуст: {audit}"
        actions = {row.get("action") for row in audit}
        assert "warn" in actions, f"в админ-логе нет записи о warn: {actions}"
        warn_row = next(r for r in audit if r.get("action") == "warn")
        assert warn_row.get("actor") == alice.username, f"актор в логе: {warn_row}"
        assert warn_row.get("target") == bob.username, f"цель в логе: {warn_row}"
    await report.step("l", "админ-лог содержит записи о действиях", scenario_l)

    # ---------------- m) журнал БД = WAL ----------------
    def scenario_m():
        db_path = need("db_path", "нет пути к БД")
        conn = sqlite3.connect(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal", f"journal_mode = {mode!r}, ожидался 'wal'"
        # следы WAL на диске: -wal/-shm живут, пока открыта последняя коннекция
        assert os.path.exists(db_path + "-wal"), f"нет файла {db_path}-wal"
    await report.step("m", "БД в режиме WAL (journal_mode)", scenario_m)

    # ---------------- n) обрыв аплоада не оставляет .part ----------------
    def scenario_n():
        data_dir = need("data_dir", "нет DATA_DIR")
        b_id = need("b_id", "нет id B")
        cookie = alice.session_cookie()

        boundary = "----selftest-abort"
        head = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"recipient_id\"\r\n\r\n{b_id}\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"group_id\"\r\n\r\n0\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"reply_to_id\"\r\n\r\n0\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"text\"\r\n\r\naborted\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"files\"; filename=\"aborted.bin\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        # заявляем 4 МБ, реально отдаём ~1 МБ и рвём соединение на середине
        declared = len(head) + 4 * 1024 * 1024 + len(f"\r\n--{boundary}--\r\n")
        headers = (
            f"POST /api/send HTTP/1.1\r\n"
            f"Host: {HOST}:{PORT}\r\n"
            f"Cookie: {cookie}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"Content-Length: {declared}\r\n\r\n"
        ).encode()
        payload = headers + head + b"A" * (1024 * 1024)
        uploads = Path(need("uploads_dir", "нет каталога uploads"))
        before = {p.name for p in uploads.iterdir() if p.is_file()}

        def parts_left():
            return sorted(str(p) for p in Path(data_dir).rglob("*.part"))

        # 1) отдаём начало потока и рвём соединение на середине (RST вместо FIN)
        sock = socket.create_connection((HOST, PORT), timeout=5)
        try:
            sock.sendall(payload[: len(headers) + len(head) + 256 * 1024])
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
        except OSError:
            pass

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and parts_left():
            time.sleep(0.1)
        assert not parts_left(), f"после обрыва соединения остались .part: {parts_left()}"

        # 2) превышение лимита: сервер пишет .part и обязан его убрать сам
        oversize = b"\x89PNG\r\n\x1a\n" + b"P" * (6 * 1024 * 1024)  # лимит на картинку темы — 5 МБ
        status, raw = alice.request(
            "/api/theme/image/wallpaper",
            files=[("image", "oversize.png", oversize, "image/png")],
        )
        assert status == 400, f"превышение лимита: HTTP {status}: {raw[:160]!r}"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and parts_left():
            time.sleep(0.1)
        assert not parts_left(), f"после превышения лимита остались .part: {parts_left()}"

        # 3) ни один из двух сбоев не оставил мусора в uploads
        after = {p.name for p in uploads.iterdir() if p.is_file()}
        assert after == before, f"в uploads появились лишние файлы: {after - before}"
    await report.step("n", "обрыв аплоада: .part не остаётся", scenario_n)

    # ---------------- o) cleanup-orphans удаляет сирот и считает их ----------------
    def scenario_o():
        uploads = Path(need("uploads_dir", "нет каталога uploads"))
        att_name = need("att_name", "нет имени вложения из сценария g")

        planted = []
        for _ in range(2):
            orphan = uploads / f"orphan-{uuid.uuid4().hex}.bin"
            orphan.write_bytes(b"orphan payload")
            planted.append(orphan)

        result = alice.json("/api/admin/cleanup-orphans")
        assert result.get("deleted") == 2, f"ожидали deleted=2, ответ: {result}"
        assert not any(p.exists() for p in planted), f"сироты остались на диске: {planted}"
        assert (uploads / att_name).exists(), f"чистка съела живое вложение {att_name}"

        again = alice.json("/api/admin/cleanup-orphans")
        assert again.get("deleted") == 0, f"повторная чистка снова что-то удалила: {again}"
    await report.step("o", "cleanup-orphans удаляет сирот, счёт верный", scenario_o)

    # ---------------- p) пульс отдаёт метрики ----------------
    def scenario_p():
        pulse = alice.json("/api/admin/pulse", method="GET")
        metrics = pulse.get("metrics")
        assert isinstance(metrics, dict), f"в ответе пульса нет metrics: {pulse}"
        assert isinstance(pulse.get("events"), list), f"в ответе пульса нет events: {pulse}"

        for key in ("disk_free_bytes", "db_size_bytes", "uptime_seconds",
                    "ws_connections", "errors_last_hour", "last_integrity", "last_backup_at"):
            assert key in metrics, f"в metrics нет поля {key}: {metrics}"

        uptime = metrics["uptime_seconds"]
        assert isinstance(uptime, (int, float)) and uptime > 0, f"uptime_seconds: {uptime!r}"
        ws = metrics["ws_connections"]
        assert isinstance(ws, int) and ws >= 2, f"ws_connections: {ws!r} (ожидали >= 2 — A и B online)"
        disk_free = metrics["disk_free_bytes"]
        assert isinstance(disk_free, int) and disk_free > 0, f"disk_free_bytes: {disk_free!r}"
        db_size = metrics["db_size_bytes"]
        assert isinstance(db_size, int) and db_size > 0, f"db_size_bytes: {db_size!r}"
        errors = metrics["errors_last_hour"]
        assert isinstance(errors, int) and errors >= 0, f"errors_last_hour: {errors!r}"

        integrity = metrics["last_integrity"]
        assert isinstance(integrity, dict) and integrity.get("ok") is True, f"last_integrity: {integrity!r}"
        assert integrity.get("ts"), f"last_integrity без времени: {integrity!r}"
        # last_backup_at допустимо None (каталога бэкапов может не быть)
        assert metrics["last_backup_at"] is None or isinstance(metrics["last_backup_at"], str), \
            f"last_backup_at: {metrics['last_backup_at']!r}"
    await report.step("p", "пульс отдаёт метрики (диск/БД/uptime/WS/ошибки)", scenario_p)

    # ---------------- q) логи: app.log с стартом, error.log ловит бум ----------------
    def scenario_q():
        data_dir = need("data_dir", "нет DATA_DIR")
        logs_dir = Path(data_dir) / "logs"
        app_log, err_log = logs_dir / "app.log", logs_dir / "error.log"

        assert app_log.is_file(), f"нет файла {app_log} (логи не настроены)"
        app_text = app_log.read_text(encoding="utf-8", errors="replace")
        assert "старт приложения" in app_text, "в app.log нет строки старта"
        assert "journal_mode = wal" in app_text, "в app.log нет строки journal_mode"
        assert "integrity_check ok" in app_text, "в app.log нет строки integrity_check"
        assert app_text.count(" | INFO | ") > 0, "формат лога не «время | level | модуль | сообщение»"

        # намеренный бум: клиенту 500, traceback — в error.log
        status, raw = alice.request("/api/admin/debug-boom", method="POST")
        assert status == 500, f"debug-boom: HTTP {status}: {raw[:160]!r}"

        # посторонним нельзя подрывать
        status_b, _ = bob.request("/api/admin/debug-boom", method="POST")
        assert status_b == 403, f"debug-boom от не-админа: HTTP {status_b} (ожидали 403)"

        deadline = time.monotonic() + 5.0
        err_text = ""
        while time.monotonic() < deadline:
            if err_log.is_file():
                err_text = err_log.read_text(encoding="utf-8", errors="replace")
                if "DEBUG BOOM" in err_text:
                    break
            time.sleep(0.1)
        assert "DEBUG BOOM" in err_text, f"в error.log нет строки намеренного сбоя: {err_text[-400:]!r}"
        assert "Traceback" in err_text, "в error.log нет traceback"
        # метрика ошибок за час отреагировала
        assert alice.json("/api/admin/pulse", method="GET")["metrics"]["errors_last_hour"] >= 1, \
            "errors_last_hour не учитывает перехваченное исключение"
    await report.step("q", "логи: app.log со стартом, error.log ловит намеренный сбой", scenario_q)

    # ---------------- r) настройки подхватываются из .env ----------------
    def scenario_r():
        repo = _make_repo_copy()
        env_data_dir = tempfile.mkdtemp(prefix="selftest-env-data-")
        EXTRA_TEMP_DIRS.append(env_data_dir)
        (Path(repo) / ".env").write_text(
            "# комментарий игнорируется\n"
            f"SECRET_KEY=test-env-key\n"
            f"DATA_DIR={env_data_dir}\n"
            "MAX_UPLOAD_BYTES=5242880\n",
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", HOST,
             "--port", str(ENV_PORT), "--log-level", "warning"],
            cwd=repo, env=_clean_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT
            started = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(
                        f"сервер с .env упал при старте:\n{(proc.stdout.read() or '')[-1200:]}"
                    )
                try:
                    with urllib.request.urlopen(f"http://{HOST}:{ENV_PORT}/health", timeout=1) as resp:
                        if resp.status == 200:
                            started = True
                            break
                except urllib.error.HTTPError:
                    started = True
                    break
                except Exception:
                    time.sleep(0.15)
            assert started, f"сервер с .env не поднялся за {STARTUP_TIMEOUT:.0f}s"

            app_log = Path(env_data_dir) / "logs" / "app.log"
            text = ""
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if app_log.is_file():
                    text = app_log.read_text(encoding="utf-8", errors="replace")
                    if "старт приложения" in text:
                        break
                time.sleep(0.1)
            assert f"DATA_DIR={env_data_dir}" in text, \
                f"в логе нет DATA_DIR из .env: {text[-400:]!r}"
            # длина ключа ровно из .env (12 символов) — значит файл действительно прочитан
            assert "SECRET_KEY" in text and "(12 символов)" in text, \
                f"SECRET_KEY не подхвачен из .env: {text[-400:]!r}"
        finally:
            stop_server(proc)
    await report.step("r", ".env подхватывается: SECRET_KEY и DATA_DIR из файла", scenario_r)

    # ---------------- s) prod со слабым ключом не стартует ----------------
    def scenario_s():
        repo = _make_repo_copy()
        prod_data_dir = tempfile.mkdtemp(prefix="selftest-prod-data-")
        EXTRA_TEMP_DIRS.append(prod_data_dir)

        env = _clean_env()
        env["APP_MODE"] = "prod"
        env["SECRET_KEY"] = "localdev"
        env["DATA_DIR"] = prod_data_dir

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "uvicorn", "main:app", "--host", HOST,
                 "--port", str(ENV_PORT), "--log-level", "warning"],
                cwd=repo, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError("prod-guard не сработал: сервер стартовал со слабым SECRET_KEY")

        output = proc.stdout or ""
        assert proc.returncode != 0, \
            f"ожидали ненулевой exit в prod со слабым ключом, получили {proc.returncode}"
        assert "SECRET_KEY" in output, f"в выводе нет упоминания SECRET_KEY:\n{output[-600:]}"
        assert "32" in output, f"в выводе нет требования про 32 символа:\n{output[-600:]}"
    await report.step("s", "APP_MODE=prod со слабым SECRET_KEY — старт запрещён", scenario_s)

    # ---------------- t) IDOR: чужое сообщение не правится и не удаляется ----------------
    def scenario_t():
        b_id = need("b_id", "нет id B")
        sent = alice.json("/api/send", data={
            "recipient_id": b_id, "group_id": 0,
            "text": "сообщение A: править и удалять может только автор", "reply_to_id": 0,
        })
        msg_id = sent["id"]

        # B — участник диалога, но не автор и не админ
        status, raw = bob.request(f"/api/messages/{msg_id}/edit", data={"text": "подмена текста"})
        assert status == 403, f"B правит чужое сообщение: HTTP {status} (ожидали 403): {raw[:160]!r}"

        status, raw = bob.request("/api/delete-message", data={"message_id": msg_id, "mode": "all"})
        assert status == 403, f"B удаляет чужое «у всех»: HTTP {status} (ожидали 403): {raw[:160]!r}"

        # контроль: автор правит своё сообщение — пускает
        edited = alice.json(f"/api/messages/{msg_id}/edit", data={"text": "отредактировано автором"})
        assert edited.get("ok") is True, f"автор не смог отредактировать своё сообщение: {edited}"
    await report.step("t", "IDOR: чужое сообщение — не правится и не удаляется не-автором", scenario_t)

    # ---------------- u) IDOR: вложение чужого диалога недоступно ----------------
    def scenario_u():
        carol = Client("Carol", "carol@selftest.local", "selftest-pass")
        invite = alice.json("/admin/invite")
        code = invite.get("invite_code")
        assert code, f"инвайт для C не создан: {invite}"
        status, raw = carol.request("/register", data={
            "name": carol.name, "email": carol.email,
            "password": carol.password, "invite_code": code,
        })
        assert status == 303, f"регистрация C: HTTP {status}: {raw[:160]!r}"
        status, raw = carol.request("/login", data={"username": carol.email, "password": carol.password})
        assert status == 303, f"логин C: HTTP {status}: {raw[:160]!r}"
        c_id = carol.json("/api/profile", method="GET")["id"]

        sent = alice.json(
            "/api/send",
            data={"recipient_id": c_id, "group_id": 0, "text": "", "reply_to_id": 0},
            files=[("files", "secret.png", PNG_1X1)],
        )
        attachments = sent.get("attachments") or []
        assert attachments, f"в ответе /api/send нет вложений: {sent}"
        att_id = attachments[0]["id"]

        # участник диалога скачивает — ок
        status, raw = carol.request(f"/api/attachment/{att_id}", method="GET")
        assert status == 200, f"C (участник) не скачал своё вложение: HTTP {status}"

        # B в этом диалоге не участвует — ни файл, ни метаданные
        status, _ = bob.request(f"/api/attachment/{att_id}", method="GET")
        assert status in (403, 404), f"B скачал чужое вложение: HTTP {status} (ожидали 403/404)"
        status, _ = bob.request(f"/api/attachment/{att_id}/info", method="GET")
        assert status in (403, 404), f"B получил метаданные чужого вложения: HTTP {status}"
        state["carol"] = carol
    await report.step("u", "IDOR: вложение из чужого диалога недоступно (403/404)", scenario_u)

    # ---------------- v) CSRF: без токена 403, с токеном 200 ----------------
    def scenario_v():
        token = alice.csrf_cookie()
        assert token, "у A нет cookie csrftoken — сервер её не выставил"

        # 1) изменяющий POST без токена → 403
        status, raw = alice.request("/api/profile", data={"name": alice.name, "bio": "без токена"}, csrf=False)
        assert status == 403, f"POST без CSRF-токена прошёл: HTTP {status}: {raw[:160]!r}"

        # 2) токен полем формы (HTML-форма / multipart) → 200
        alice.json(
            "/api/profile",
            data={"name": alice.name, "bio": "csrf-ok-form", "csrf_token": token},
            csrf=False,
        )
        profile = alice.json("/api/profile", method="GET")
        assert profile.get("bio") == "csrf-ok-form", f"bio не применилось (форма): {profile}"

        # 3) токен заголовком X-CSRF-Token → 200
        alice.json("/api/profile", data={"name": alice.name, "bio": "csrf-ok-header"})
        profile = alice.json("/api/profile", method="GET")
        assert profile.get("bio") == "csrf-ok-header", f"bio не применилось (заголовок): {profile}"
    await report.step("v", "CSRF: без токена 403, токен в поле формы и в заголовке — 200", scenario_v)

    # ---------------- w) админ-эндпоинты закрыты от не-админа ----------------
    def scenario_w():
        endpoints = (
            ("/api/admin/invites", "GET"),
            ("/api/admin/audit", "GET"),
            ("/api/admin/pulse", "GET"),
            ("/api/admin/cleanup-orphans", "POST"),
        )
        for path, method in endpoints:
            status, raw = bob.request(path, method=method)
            assert status == 403, \
                f"B (не админ) → {method} {path}: HTTP {status} (ожидали 403): {raw[:160]!r}"
        # контроль: админ видит те же эндпоинты
        for path, method in (("/api/admin/invites", "GET"), ("/api/admin/audit", "GET")):
            status, _ = alice.request(path, method=method)
            assert status == 200, f"A (админ) → {method} {path}: HTTP {status} (ожидали 200)"
    await report.step("w", "админ-эндпоинты недоступны не-админу (403)", scenario_w)

    # ---------------- x) смена пароля по коду из письма ----------------
    def scenario_x():
        data_dir = need("data_dir", "нет DATA_DIR сервера")
        # отдельный пользователь, чтобы не ломать пароли A и B
        dave = Client("Dave", "dave@selftest.local", "dave-old-pass")
        invite = alice.json("/admin/invite")
        status, raw = dave.request("/register", data={
            "name": dave.name, "email": dave.email,
            "password": dave.password, "invite_code": invite["invite_code"],
        })
        assert status == 303, f"регистрация D: HTTP {status}: {raw[:160]!r}"
        assert dave.request("/login", data={"username": dave.email, "password": dave.password})[0] == 303

        # вторая сессия того же пользователя (второе «устройство»)
        dave2 = Client(dave.name, dave.email, dave.password)
        assert dave2.request("/login", data={"username": dave.email, "password": dave.password})[0] == 303
        assert dave2.json("/api/profile", method="GET")["id"] == dave.json("/api/profile", method="GET")["id"]

        # шаг 1: просим код (текущий пароль проверяется на сервере)
        req = dave.json("/api/email/code/request", data={
            "purpose": "password_change", "current_password": dave.password,
        })
        assert req.get("success") is True, f"запрос кода: {req}"
        code = last_email_code(data_dir, purpose="password_change")

        # шаг 2: код + новый пароль
        new_password = "dave-new-pass"
        changed = dave.json("/api/profile/password", data={
            "current_password": dave.password, "new_password": new_password,
            "confirm_password": new_password, "code": code,
        })
        assert changed.get("success") is True, f"смена пароля: {changed}"

        # старый пароль больше не пускает, новый — пускает
        assert dave2.request("/login", data={"username": dave.email, "password": dave.password})[0] == 200, \
            "старый пароль всё ещё принимается"
        fresh = Client(dave.name, dave.email, new_password)
        assert fresh.request("/login", data={"username": dave.email, "password": new_password})[0] == 303, \
            "новый пароль не принят"

        # вторая сессия порвана (session_epoch++ из R6), текущая — жива
        status, _ = dave2.request("/api/profile", method="GET")
        assert status == 401, f"вторая сессия жива после смены пароля: HTTP {status}"
        status, _ = dave.request("/api/profile", method="GET")
        assert status == 200, f"текущая сессия умерла после смены пароля: HTTP {status}"
        state["dave"] = dave
    await report.step("x", "смена пароля по коду: старый пароль мёртв, вторая сессия порвана", scenario_x)

    # ---------------- y) политика кодов: 5 попыток и 1 код/60с ----------------
    def scenario_y():
        data_dir = need("data_dir", "нет DATA_DIR сервера")
        # цель verify на B: отдельный лимит на (user, purpose)
        bob.json("/api/email/code/request", data={"purpose": "verify"})
        code = last_email_code(data_dir, user_id=bob.id, purpose="verify")
        wrong = "000000" if code != "000000" else "111111"

        for attempt in range(1, 6):
            status, raw = bob.request("/api/email/code/confirm",
                                      data={"purpose": "verify", "code": wrong})
            assert status == 400, f"неверный код #{attempt}: HTTP {status} (ожидали 400): {raw[:160]!r}"

        # 6-я попытка — код уже заблокирован (инвалидирован)
        status, raw = bob.request("/api/email/code/confirm", data={"purpose": "verify", "code": code})
        assert status in (400, 429), f"код жив после 5 неверных попыток: HTTP {status}: {raw[:160]!r}"

        # повторная отправка раньше 60с → 429
        status, raw = bob.request("/api/email/code/request", data={"purpose": "verify"})
        assert status == 429, f"повторная отправка кода: HTTP {status} (ожидали 429): {raw[:160]!r}"
    await report.step("y", "политика кодов: 5 неверных → блок, повтор раньше 60с → 429", scenario_y)

    # ---------------- z) смена почты по коду + email_verified ----------------
    def scenario_z():
        data_dir = need("data_dir", "нет DATA_DIR сервера")
        new_email = "bob-new@selftest.local"
        req = bob.json("/api/email/code/request", data={
            "purpose": "email_change", "new_email": new_email, "current_password": bob.password,
        })
        assert req.get("success") is True, f"запрос кода на новую почту: {req}"
        code = last_email_code(data_dir, user_id=bob.id, purpose="email_change")

        before = bob.json("/api/profile", method="GET")
        assert before.get("email_verified") == 0, f"почта уже верифицирована до подтверждения: {before}"

        changed = bob.json("/api/profile/email", data={"email": new_email, "code": code})
        assert changed.get("success") is True, f"смена почты: {changed}"

        after = bob.json("/api/profile", method="GET")
        assert after.get("email") == new_email, f"почта не обновилась: {after}"
        assert after.get("email_verified") == 1, f"email_verified не выставлен: {after}"

        # логин теперь по новой почте
        status, _ = bob.request("/login", data={"username": new_email, "password": bob.password})
        assert status == 303, f"логин по новой почте: HTTP {status}"
    await report.step("z", "смена почты по коду: адрес обновлён, email_verified=1", scenario_z)

    # ---------------- aa) онбординг: приветствие от канала start ----------------
    def scenario_aa():
        # C — свежий пользователь: приветствие от «start» приходит само
        carol = state.get("carol")
        assert carol is not None, "нет третьего пользователя из сценария u"
        start_id = carol.json("/api/profile", method="GET")["system_user_id"]
        assert start_id, "сервер не сообщил id системного канала"

        history = dialog_history(carol, start_id)
        assert history, f"у нового пользователя нет сообщений от канала start: {history}"
        welcome = history[0]
        assert welcome.get("is_system") == 1, f"нет флага центрирования is_system: {welcome}"
        assert "ВайбБункер" in (welcome.get("text") or ""), f"текст не похож на приветствие: {welcome}"[:200]

        # канал сверху списка контактов и помечен системным
        contacts = carol.json("/api/users", method="GET")
        assert contacts and contacts[0].get("is_system") == 1, \
            f"канал start не первый в списке контактов: {[c.get('name') for c in contacts]}"
        state["start_id"] = start_id
    await report.step("aa", "онбординг: приветствие от канала start, флаг центрирования", scenario_aa)

    # ---------------- bb) писать в канал может только creator ----------------
    async def scenario_bb():
        start_id = need("start_id", "нет id канала start")
        # обычный пользователь → 403
        status, raw = bob.request("/api/send", data={
            "recipient_id": start_id, "group_id": 0, "text": "привет, канал", "reply_to_id": 0,
        })
        assert status == 403, f"B (не creator) пишет в канал: HTTP {status} (ожидали 403): {raw[:160]!r}"

        # админ, но не creator → тоже 403
        eve = Client("Eve", "eve@selftest.local", "selftest-pass")
        invite = alice.json("/admin/invite")
        status, raw = eve.request("/register", data={
            "name": eve.name, "email": eve.email,
            "password": eve.password, "invite_code": invite["invite_code"],
        })
        assert status == 303, f"регистрация E: HTTP {status}: {raw[:160]!r}"
        assert eve.request("/login", data={"username": eve.email, "password": eve.password})[0] == 303
        alice.json("/admin/grant-admin", data={"target_user_id": eve.json("/api/profile", method="GET")["id"]})
        status, raw = eve.request("/api/send", data={
            "recipient_id": start_id, "group_id": 0, "text": "я админ, пусти", "reply_to_id": 0,
        })
        assert status == 403, f"админ-не-creator пишет в канал: HTTP {status} (ожидали 403): {raw[:160]!r}"

        # creator → 200, и это уходит всем (broadcast)
        carol = need("carol", "нет третьего пользователя")
        ws_c = await ws_open(carol)
        try:
            sent = alice.json("/api/send", data={
                "recipient_id": start_id, "group_id": 0,
                "text": "ОБЪЯВЛЕНИЕ: вечером деплой", "reply_to_id": 0,
            })
            assert sent.get("broadcast", 0) >= 2, f"объявление не разослано: {sent}"
            assert sent.get("is_system") is True, f"объявление не помечено системным: {sent}"

            event = await expect_event(
                ws_c,
                lambda ev: ev.get("type") == "message" and ev.get("text") == "ОБЪЯВЛЕНИЕ: вечером деплой",
                "объявление канала пришло слушателю",
            )
            assert event.get("is_system") is True, f"событие без флага центрирования: {event}"

            history = dialog_history(carol, start_id)
            assert any(m.get("text") == "ОБЪЯВЛЕНИЕ: вечером деплой" and m.get("is_system") == 1
                       for m in history), f"объявления нет в истории слушателя: {history}"
        finally:
            await ws_c.close()
        state["eve"] = eve
    await report.step("bb", "канал start: не-creator → 403, creator → объявление всем", scenario_bb)

    # ---------------- cc) канал нельзя удалить, профиль — только creator ----------------
    def scenario_cc():
        start_id = need("start_id", "нет id канала start")
        # delete-chat для канала запрещён
        status, raw = bob.request("/api/delete-chat", data={"recipient_id": start_id})
        assert status == 403, f"delete-chat канала: HTTP {status} (ожидали 403): {raw[:160]!r}"
        status, raw = alice.request("/api/delete-chat", data={"recipient_id": start_id})
        assert status == 403, f"delete-chat канала от creator: HTTP {status} (ожидали 403)"

        # приветствие всё ещё на месте — «занавес» ничего не стёр
        history = dialog_history(bob, start_id)
        assert history, "чат с каналом опустел — удаление прошло"

        # профиль канала: creator правит, остальные — нет
        edited = alice.json("/api/profile", data={"name": "ВайбБункер", "bio": "канал объявлений",
                                                  "target_user_id": start_id})
        assert edited.get("success") is True, f"creator не смог оформить канал: {edited}"

        status, raw = bob.request("/api/profile", data={"name": "Чужой канал", "bio": "взлом",
                                                       "target_user_id": start_id})
        assert status == 403, f"не-creator правит канал: HTTP {status} (ожидали 403): {raw[:160]!r}"

        eve = state.get("eve")
        if eve is not None:
            status, raw = eve.request("/api/profile", data={"name": "Админский канал", "bio": "взлом",
                                                            "target_user_id": start_id})
            assert status == 403, f"админ-не-creator правит канал: HTTP {status} (ожидали 403): {raw[:160]!r}"

        # оформление применилось
        channel = bob.json(f"/api/user/{start_id}/profile", method="GET")
        assert channel.get("is_system") == 1, f"профиль канала без флага is_system: {channel}"
        assert channel.get("bio") == "канал объявлений", f"описание канала не обновилось: {channel}"
    await report.step("cc", "канал: удаление запрещено, профиль правит только creator", scenario_cc)

    # ---------------- закрытие WS ----------------
    for ws in (state.get("ws_a"), state.get("ws_b")):
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    busy = [port for port in (PORT, ENV_PORT) if port_is_busy(port)]
    if busy:
        print(f"FAIL предварительная проверка: порт(ы) {', '.join(map(str, busy))} заняты — "
              f"selftest не трогает чужие серверы. Освободите их и повторите.")
        print("SELFTEST: 0/0 OK")
        return 1

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("FAIL предварительная проверка: в текущем интерпретаторе нет uvicorn. "
              "Запускайте из venv: ./.venv/bin/python scripts/selftest.py")
        print("SELFTEST: 0/0 OK")
        return 1

    data_dir = tempfile.mkdtemp(prefix="messenger-selftest-")
    proc: subprocess.Popen | None = None
    report = Report()
    print(f"Запуск selftest: PORT={PORT}, DATA_DIR={data_dir}")
    print("-" * 72)

    try:
        try:
            proc = start_server(data_dir)
        except Exception as exc:
            print(f"FAIL старт сервера: {exc}")
            print("SELFTEST: 0/0 OK")
            return 1

        # изоляция: БД и вложения обязаны лежать во временном DATA_DIR,
        # а не в ./data рабочего инстанса
        expected_db = Path(data_dir) / "messenger.db"
        if not expected_db.exists():
            print(f"FAIL изоляция данных: {expected_db} не создан — "
                  f"DATA_DIR не respected")
            print("SELFTEST: 0/0 OK")
            return 1

        asyncio.run(run_scenarios(report, data_dir))
    finally:
        log = stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)
        for path in EXTRA_TEMP_DIRS:
            shutil.rmtree(path, ignore_errors=True)
        # лог сервера печатаем только когда что-то упало: иначе это шум миграций
        if log.strip() and report.failed:
            tail = "\n".join(log.strip().splitlines()[-15:])
            print("-" * 72)
            print(f"лог сервера (хвост):\n{tail}")

    print("-" * 72)
    passed = report.total - report.failed
    print(f"SELFTEST: {passed}/{report.total} OK "
          f"[{time.monotonic() - report.t0:.1f}s, DATA_DIR очищен: {not os.path.exists(data_dir)}]")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
