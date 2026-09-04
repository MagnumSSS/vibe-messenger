#!/usr/bin/env python3
"""
Phase R2: онлайн-бэкап SQLite.

В отличие от копирования файла, используется sqlite3.Connection.backup() —
тот самый Online Backup API: копия снимается консистентно, без остановки сервиса
и без ручной возни с WAL. После копирования бэкап проверяется (PRAGMA integrity_check
по КОПИИ, не по источнику): битый результат — ненулевой exit и явное сообщение.

Env (совместимы со старым backup.sh и cron):
    DATA_DIR      — где лежит messenger.db (default /var/lib/messenger/data)
    BACKUP_DIR    — куда складываем (default /var/lib/messenger/backups)
    DB_NAME       — имя файла БД (default messenger.db)
    RETAIN_COUNT  — сколько последних копий хранить (default 5)
    PYTHON        — не используется здесь, нужен shim'у backup.sh

Exit-коды:
    0 — всё ok
    2 — ошибка ввода-вывода/окружения (нет БД, нельзя создать каталог)
    3 — копия не прошла integrity_check
    4 — сбой Online Backup API
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

EXIT_OK = 0
EXIT_IO = 2
EXIT_INTEGRITY = 3
EXIT_BACKUP = 4

DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/messenger/data")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/var/lib/messenger/backups")
DB_NAME = os.environ.get("DB_NAME", "messenger.db")
RETAIN_COUNT = int(os.environ.get("RETAIN_COUNT", "5"))


def log(message: str) -> None:
    print(message, flush=True)


def integrity_report(path: Path) -> tuple[bool, str]:
    """PRAGMA integrity_check по указанному файлу БД."""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return False, f"integrity_check не выполнился: {exc}"
    finally:
        conn.close()
    messages = [str(row[0]) for row in rows]
    return messages == ["ok"], "; ".join(messages)[:500]


def online_copy(src: Path, dst: Path) -> tuple[float, int]:
    """sqlite3.backup() — онлайн-копия; возвращает (секунды, страницы)."""
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        started = time.monotonic()
        source.backup(target)          # Online Backup API: WAL-safe, сервис не останавливаем
        elapsed = time.monotonic() - started
        pages = target.execute("PRAGMA page_count").fetchone()[0]
        return elapsed, pages
    finally:
        target.close()
        source.close()


def archive_uploads(data_dir: Path, tar_path: Path) -> bool:
    """tar.gz каталога uploads (если он есть)."""
    uploads = data_dir / "uploads"
    if not uploads.is_dir():
        log(f"WARNING: каталог вложений не найден: {uploads}")
        return False
    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(uploads), arcname="uploads")
    return True


def apply_retention(backup_dir: Path, pattern: str, keep: int) -> int:
    files = sorted(backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    verify_only = None
    if args:
        if args[0] == "--verify" and len(args) == 2:
            verify_only = Path(args[1])
        else:
            print(f"Использование: {Path(__file__).name} [--verify ФАЙЛ_КОПИИ]")
            return EXIT_IO

    # -- режим проверки существующей копии (cron/ручная валидация) --
    if verify_only is not None:
        if not verify_only.is_file():
            log(f"ERROR: файл копии не найден: {verify_only}")
            return EXIT_IO
        ok, detail = integrity_report(verify_only)
        log(f"integrity_check {verify_only.name}: {detail}")
        if not ok:
            log(f"ERROR: копия {verify_only} битая — восстановление из неё невозможно")
            return EXIT_INTEGRITY
        log("Копия целая, восстановление возможно")
        return EXIT_OK

    data_dir = Path(DATA_DIR)
    backup_dir = Path(BACKUP_DIR)
    db_path = data_dir / DB_NAME

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_db = backup_dir / f"messenger-{timestamp}.db"
    backup_tar = backup_dir / f"uploads-{timestamp}.tar.gz"

    log(f"Starting backup at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Data directory: {data_dir}")
    log(f"Backup directory: {backup_dir}")

    if not db_path.is_file():
        log(f"ERROR: база не найдена: {db_path}")
        return EXIT_IO
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"ERROR: не удалось создать каталог бэкапов: {exc}")
        return EXIT_IO

    # 1) онлайн-копия БД
    log("Backing up database (sqlite3 backup API)...")
    try:
        elapsed, pages = online_copy(db_path, backup_db)
    except sqlite3.Error as exc:
        log(f"ERROR: сбой Online Backup API: {exc}")
        try:
            backup_db.unlink()
        except OSError:
            pass
        return EXIT_BACKUP
    log(f"Database backup created: {backup_db} ({pages} страниц, {elapsed:.2f}s)")

    # 2) проверка именно копии: бэкап, который нельзя восстановить, не считается бэкапом
    ok, detail = integrity_report(backup_db)
    log(f"integrity_check копии: {detail}")
    if not ok:
        log(f"ERROR: копия {backup_db} не прошла integrity_check — бэкап недействителен")
        return EXIT_INTEGRITY

    # 3) вложения
    log("Backing up uploads...")
    if archive_uploads(data_dir, backup_tar):
        log(f"Uploads backup created: {backup_tar}")
    else:
        backup_tar = None

    # 4) ротация
    removed_db = apply_retention(backup_dir, "messenger-*.db", RETAIN_COUNT)
    removed_tar = apply_retention(backup_dir, "uploads-*.tar.gz", RETAIN_COUNT)
    log(f"Cleaning old backups (keeping last {RETAIN_COUNT}): -{removed_db} db, -{removed_tar} tar")

    log(f"Backup completed successfully at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("")
    log("To restore on a clean machine:")
    log(f"  1. Copy {backup_db} to $DATA_DIR/{DB_NAME}")
    if backup_tar:
        log(f"  2. Extract {backup_tar} to $DATA_DIR/")
        log("  3. Start the messenger service")
    else:
        log("  2. Start the messenger service")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
