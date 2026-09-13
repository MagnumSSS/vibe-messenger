#!/usr/bin/env python3
"""
Записать событие в data/logs/app.log тем же форматом и той же ротацией, что у приложения.

Нужно, чтобы события обновления (update.sh) лежали в общем логе рядом
с стартовыми, WS, аплоадами и админ-действиями.

Использование:
    DATA_DIR=/var/lib/messenger/data python3 scripts/log_event.py update "UPDATE OK"

Секреты (cookie, SECRET_KEY, пароли) в аргументы передавать нельзя — они уйдут в лог.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(module)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"Использование: {os.path.basename(argv[0])} <тип> <сообщение>", file=sys.stderr)
        return 2

    kind, message = argv[1], argv[2]
    data_dir = os.environ.get("DATA_DIR", "data")
    log_dir = os.path.join(data_dir, "logs")

    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        print(f"не удалось создать каталог логов: {exc}", file=sys.stderr)
        return 1

    logger = logging.getLogger("messenger.update")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        logger.addHandler(handler)
    logger.propagate = False

    logger.info("%s: %s", kind, message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
