#!/usr/bin/env python3
"""
Минимальный загрузчик .env — только stdlib, без внешних зависимостей.

Правила:
  * читается .env из корня репозитория (если файла нет — молча ничего не делаем);
  * формат KEY=VALUE, `#` — комментарий (строчный и хвостовой у нецитируемых значений);
  * значения можно брать в одинарные или двойные кавычки;
  * УЖЕ существующие переменные окружения не перезаписываются:
    явный запуск с переменными всегда важнее файла.
"""

from __future__ import annotations

import os
from pathlib import Path

DOTENV_NAME = ".env"


def parse(text: str) -> dict:
    """Разобрать содержимое .env в словарь."""
    values: dict = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, raw_value = line.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ("'", '"'):
            value = raw_value[1:-1]          # кавычки снимаем, содержимое не трогаем
        else:
            value = raw_value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        values[key] = value
    return values


def load(path=None, override: bool = False) -> dict:
    """
    Прочитать .env в os.environ.

    Возвращает словарь применённых значений (ключ → значение).
    Существующие переменные окружения не перезаписываются, если только
    не передан override=True.
    """
    env_path = Path(path) if path else Path(__file__).resolve().parents[1] / DOTENV_NAME
    applied: dict = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return applied
    for key, value in parse(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


if __name__ == "__main__":
    applied = load()
    if applied:
        for key in applied:
            print(f"{key} <- .env")
    else:
        print(f"{DOTENV_NAME} не найден или все значения уже заданы в окружении")
