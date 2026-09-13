# ВайбБункер — деплой на VPS (фаза 7.5a)

Пошаговый runbook, воспроизводимый **с нуля на чистой Debian 12**.

| Параметр | Значение |
|---|---|
| IP | `171.22.130.120` |
| ОС | Debian 12 (bookworm) |
| Домен | `vibebunker.ru` (+ `www`), DNS у reg.ru |
| HTTPS | Let's Encrypt (certbot --nginx) |
| Пользователь приложения | `vibebunker` (sudo, вход только по ключу) |
| Каталог приложения | `/home/vibebunker/vibe_mes` |
| Данные | чистый старт (БД не переносим) |
| Почта | SMTP позже — плейсхолдеры в `.env`, коды пока отдают 503 |

Обозначения: `local$` — команда **на твоём компьютере**, `root#` — на сервере от root,
`vibe$` — на сервере от `vibebunker`.

> **Перед шагом 8** в панели reg.ru должны стоять A-записи:
> `vibebunker.ru → 171.22.130.120` и `www.vibebunker.ru → 171.22.130.120`.
> Проверка: `local$ dig +short vibebunker.ru` → `171.22.130.120`.

---

## Шаг 1. Первый вход root и немедленная смена пароля

Пароль от хостера считаем скомпрометированным (он ходил по переписке), поэтому первое
действие — смена, ещё до всего остального.

```bash
local$ ssh root@171.22.130.120
root# passwd
```

Ожидаемый вывод:

```
New password:
Retype new password:
passwd: password updated successfully
```

Пароль — 20+ символов из менеджера паролей. Он понадобится ровно один раз (как страховка
через VNC-консоль хостера), дальше вход будет только по ключу.

Заодно освежим систему:

```bash
root# apt-get update -qq && apt-get -y -qq upgrade
root# cat /etc/os-release | head -2
```

Ожидаемо: `PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"`.

---

## Шаг 2. Забросить на сервер SSH-ключ и репозиторий

**2.1. Ключ.** Если пары ключей нет — создать на клиенте:

```bash
local$ ssh-keygen -t ed25519 -C "vibebunker"        # Enter, Enter, парольная фраза по желанию
local$ ssh-copy-id root@171.22.130.120
```

Ожидаемо: `Number of key(s) added: 1`. Проверка, что ключ работает **без пароля**:

```bash
local$ ssh -o PasswordAuthentication=no root@171.22.130.120 'echo KEY_OK'
KEY_OK
```

> Ключ кладём root'у сознательно: `bootstrap_server.sh` на шаге 3 скопирует его
> пользователю `vibebunker` и только после этого закроет парольный вход.

**2.2. Репозиторий.** Клонируем прямо на сервер (репо публичный):

```bash
root# apt-get install -y -qq git
root# git clone https://github.com/MagnumSSS/vibe-messenger.git /root/vibe_mes_src
root# cd /root/vibe_mes_src && git log --oneline -1
```

Если репозиторий приватный и `gh`/токена на сервере нет — залей архивом с клиента:

```bash
local$ rsync -az --exclude .git --exclude .venv --exclude data ./ root@171.22.130.120:/root/vibe_mes_src/
```

---

## Шаг 3. Прогон bootstrap_server.sh

```bash
root# cd /root/vibe_mes_src
root# bash deploy/bootstrap_server.sh
```

Скрипт идемпотентен: повторный прогон печатает `skip` вместо повторной работы.
Ожидаемый хвост вывода:

```
[8] SSH lockdown (root-вход и пароли — запретить)
    ok   закомментированы конкурирующие строки в /etc/ssh/sshd_config
    ok   sshd перечитан; активная политика:
      port 22
      permitrootlogin no
      passwordauthentication no
      permitemptypasswords no

--------------------------------------------------------------------
BOOTSTRAP OK
  пользователь : vibebunker (sudo), каталог приложения: /home/vibebunker/vibe_mes
  ключей SSH   : 1
  файрвол      : ufw allow 22/80/443, остальное deny
  fail2ban     : sshd, maxretry=5, bantime=1h
  sshd         : пароли и root-вход ЗАКРЫТЫ
--------------------------------------------------------------------
```

Если в блоке `[8]` написано `ВНИМАНИЕ: у vibebunker нет SSH-ключа` — значит ключа не нашлось,
пароли **намеренно** оставлены включёнными. Сделай `local$ ssh-copy-id vibebunker@171.22.130.120`
и прогони скрипт ещё раз.

Что должно быть после шага (можно проверить сразу):

```bash
root# ufw status
Status: active
To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
80/tcp                     ALLOW       Anywhere
443/tcp                    ALLOW       Anywhere

root# fail2ban-client status sshd
Status for the jail: sshd
|- Filter
|  `- Currently failed: 0
`- Actions
   `- Currently banned: 0

root# free -h | grep -i swap        # при RAM < 2G
Swap:          1.0Gi          0B       1.0Gi

root# timedatectl | grep "Time zone"
Time zone: Europe/Moscow (MSK, +0300)
```

---

## Шаг 4. Перелогиниться ключом + ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА

**Не закрывай текущую root-сессию**, пока следующая проверка не пройдёт — это твой
запасной выход.

В новом терминале:

```bash
local$ ssh vibebunker@171.22.130.120 'whoami && sudo -n true && echo SUDO_OK'
vibebunker
SUDO_OK
```

(`sudo` может спросить пароль — у `vibebunker` его нет; тогда дай доступ без пароля:
`root# echo 'vibebunker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/vibebunker && chmod 440 /etc/sudoers.d/vibebunker`,
либо задай пароль `root# passwd vibebunker` и вводи его руками.)

Теперь **отрицательная проверка приёмки** — вход по паролю должен быть невозможен:

```bash
local$ ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password vibebunker@171.22.130.120
vibebunker@171.22.130.120: Permission denied (publickey).

local$ ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@171.22.130.120
root@171.22.130.120: Permission denied (publickey).
```

Оба ответа `Permission denied (publickey)` **без запроса пароля** — критерий приёмки.
Если появилось приглашение `password:` — sshd не применил дропин: смотри
`root# sshd -T | grep -i passwordauthentication` и `/etc/ssh/sshd_config.d/99-vibebunker.conf`.

Только после этого можно закрывать root-сессию.

---

## Шаг 5. Код на место, venv и зависимости

```bash
vibe$ sudo rsync -a --exclude .venv --exclude data /root/vibe_mes_src/ /home/vibebunker/vibe_mes/
vibe$ sudo chown -R vibebunker:vibebunker /home/vibebunker/vibe_mes
vibe$ cd /home/vibebunker/vibe_mes
vibe$ python3 -m venv .venv
vibe$ .venv/bin/pip install --upgrade pip -q
vibe$ .venv/bin/pip install -q -r requirements.txt
vibe$ .venv/bin/python -c "import fastapi, uvicorn, websockets; print('DEPS OK')"
DEPS OK
```

Каталоги данных и бэкапов:

```bash
vibe$ mkdir -p /home/vibebunker/vibe_mes/data/logs /home/vibebunker/backups
vibe$ chmod 750 /home/vibebunker/backups
```

Быстрый прогон брони прямо на сервере (полезно: ловит кривые зависимости):

```bash
vibe$ .venv/bin/python scripts/selftest.py | tail -3
------------------------------------------------------------------------
SELFTEST: 31/31 OK [~3s, DATA_DIR очищен: True]
```

---

## Шаг 6. `.env` и секреты

```bash
vibe$ cd /home/vibebunker/vibe_mes
vibe$ cp .env.prod.example .env
vibe$ chmod 600 .env
vibe$ .venv/bin/python -c "import secrets;print(secrets.token_hex(32))"
3f9c...64 hex-символа...
vibe$ nano .env        # вставить ключ в SECRET_KEY=
```

Обязательный минимум в `.env`: `APP_MODE=prod`, `SECRET_KEY=<64 символа>`, `PORT=8000`,
`DATA_DIR=/home/vibebunker/vibe_mes/data`, `BACKUP_DIR=/home/vibebunker/backups`.
Кавычек и пробелов вокруг `=` быть не должно — файл читает и systemd тоже.

> На шаге 6 в `.env` уже стоит `SESSION_SECURE=1`. Пока сертификата нет (до шага 8),
> вход по `http://` работать не будет — это ожидаемо. Если хочешь проверить вход
> по IP до certbot, временно поставь `SESSION_SECURE=0` и верни `1` после шага 8.

Проверка гварда прод-режима (он должен ругаться на пустой ключ — убедимся, что ключ принят):

```bash
vibe$ .venv/bin/python -c "
import scripts.load_env as e, os; e.load(); print('APP_MODE=', os.environ['APP_MODE'], 'len(SECRET_KEY)=', len(os.environ['SECRET_KEY']))"
APP_MODE= prod len(SECRET_KEY)= 64
```

---

## Шаг 7. systemd-юнит

```bash
vibe$ sudo cp deploy/messenger.service /etc/systemd/system/messenger.service
vibe$ sudo systemctl daemon-reload
vibe$ sudo systemctl enable --now messenger
vibe$ systemctl status messenger --no-pager | head -8
```

Ожидаемо:

```
● messenger.service - VibeBunker private messenger (FastAPI/uvicorn)
     Loaded: loaded (/etc/systemd/system/messenger.service; enabled; preset: enabled)
     Active: active (running) since ...
```

Локальная проверка до nginx:

```bash
vibe$ curl -s http://127.0.0.1:8000/health
{"status":"ok",...}
```

Логи при проблемах: `journalctl -u messenger -n 50 --no-pager` и
`tail -n 50 /home/vibebunker/vibe_mes/data/logs/error.log`.

---

## Шаг 8. nginx + HTTPS (Let's Encrypt)

```bash
vibe$ sudo mkdir -p /var/www/certbot
vibe$ sudo cp deploy/nginx_vibebunker.conf /etc/nginx/sites-available/vibebunker
vibe$ sudo ln -sf /etc/nginx/sites-available/vibebunker /etc/nginx/sites-enabled/vibebunker
vibe$ sudo rm -f /etc/nginx/sites-enabled/default
vibe$ sudo nginx -t
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
vibe$ sudo systemctl reload nginx
```

Проверка, что по http уже отвечает приложение (DNS должен быть прописан):

```bash
vibe$ curl -s http://vibebunker.ru/health
{"status":"ok",...}
```

Сертификат:

```bash
vibe$ sudo certbot --nginx -d vibebunker.ru -d www.vibebunker.ru --agree-tos -m you@example.com --redirect
```

Ожидаемо:

```
Successfully received certificate.
Certificate is saved at: /etc/letsencrypt/live/vibebunker.ru/fullchain.pem
Deploying certificate
Successfully deployed certificate for vibebunker.ru
Congratulations! You have successfully enabled HTTPS
```

Certbot сам допишет блок `:443` и редирект с `:80`. После него добавь руками в 443-й
`server` то, чего certbot не знает (иначе большие вложения и WebSocket будут спотыкаться):

```bash
vibe$ sudo nano /etc/nginx/sites-available/vibebunker
```

внутри `server { listen 443 ssl; ... server_name vibebunker.ru; }`:

```
    client_max_body_size 6m;
    location /ws {
        proxy_pass http://vibebunker_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }
```

и:

```bash
vibe$ sudo nginx -t && sudo systemctl reload nginx
vibe$ sudo systemctl list-timers | grep certbot     # автопродление
vibe$ sudo certbot renew --dry-run | tail -3
```

Ожидаемо: `Congratulations, all simulated renewals succeeded`.

---

## Шаг 9. Smoke-тест

С сервера:

```bash
vibe$ curl -sI https://vibebunker.ru/ | head -3
HTTP/2 200
vibe$ curl -s https://vibebunker.ru/health
{"status":"ok",...}
vibe$ curl -sI http://vibebunker.ru/ | head -2      # редирект на https
HTTP/1.1 301 Moved Permanently
```

С домашнего компьютера и с телефона (мобильный интернет, не Wi-Fi — так проверяется
реальный внешний маршрут):

1. Открыть `https://vibebunker.ru` — замок в адресной строке, без предупреждений.
2. **Зарегистрироваться первым** — этот аккаунт получает `is_admin=1` и `is_creator=1`
   (канал объявлений пишет только он). Не отдавай первую регистрацию никому.
3. Проверить: аватар грузится, отправка сообщения, WebSocket-доставка (второе устройство —
   сообщение приходит без перезагрузки), вложение ~2 МБ, presence «онлайн/оффлайн».
4. PWA: «Добавить на главный экран» на телефоне, запуск из иконки.
5. Админка → пульс: диск, БД, uptime, WS, ошибки.

Проверка WebSocket снаружи:

```bash
local$ curl -sI -o /dev/null -w '%{http_code}\n' \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://vibebunker.ru/ws
403
```

`403` (или `401`) — нормально: соединение дошло до приложения и отбито проверкой сессии.
`502`/`400 Bad Request` от nginx — значит WS-заголовки не проброшены, вернись к шагу 8.

Замечание про почту: пока `SMTP_HOST` пуст, в `APP_MODE=prod` запрос кода вернёт
**503 «почта не настроена»**. Это ожидаемое состояние до получения SMTP-доступов;
после заполнения `SMTP_*` — `sudo systemctl restart messenger`.

---

## Шаг 10. Бэкапы по cron и ротация логов

Бэкап — `scripts/backup.py`: Online Backup API + `integrity_check` копии + ротация
`RETAIN_COUNT=5`. Ручной прогон:

```bash
vibe$ cd /home/vibebunker/vibe_mes
vibe$ set -a && . ./.env && set +a && .venv/bin/python scripts/backup.py
vibe$ ls -lh /home/vibebunker/backups | tail -3
vibe$ echo $?
0
```

Ежедневно в 04:30 по Москве:

```bash
vibe$ crontab -e
```

```cron
30 4 * * * cd /home/vibebunker/vibe_mes && set -a && . ./.env && set +a && .venv/bin/python scripts/backup.py >> /home/vibebunker/vibe_mes/data/logs/backup.log 2>&1
```

Проверка: `vibe$ crontab -l` показывает строку; наутро в `data/logs/backup.log` —
`BACKUP OK` и свежий файл в `/home/vibebunker/backups`.

Логи приложения ротирует сам Python (`RotatingFileHandler`, 5 МБ × 5 в
`data/logs/app.log` и `error.log`) — logrotate для них не нужен и не должен их трогать.
Ротируем только cron-лог бэкапов и nginx (nginx-правило ставится пакетом):

```bash
vibe$ sudo tee /etc/logrotate.d/vibebunker >/dev/null <<'EOF'
/home/vibebunker/vibe_mes/data/logs/backup.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    su vibebunker vibebunker
    create 0640 vibebunker vibebunker
}
EOF
vibe$ sudo logrotate -d /etc/logrotate.d/vibebunker 2>&1 | tail -5
vibe$ ls /etc/logrotate.d/nginx && sudo logrotate -d /etc/logrotate.d/nginx >/dev/null && echo NGINX_LOGROTATE_OK
```

Ожидаемо: отладочный прогон без ошибок, `NGINX_LOGROTATE_OK`.

---

## Обновления после деплоя

Только через `scripts/update.sh` — он делает PRE-CHECK selftest → бэкап БД →
`git fetch/reset` → рестарт сервиса → POST-CHECK `/health` + selftest, и при красном
POST-CHECK откатывается на прежний коммит с восстановлением БД.

```bash
vibe$ cd /home/vibebunker/vibe_mes
vibe$ set -a && . ./.env && set +a
vibe$ PYTHON=.venv/bin/python bash scripts/update.sh --dry-run   # план без изменений
vibe$ PYTHON=.venv/bin/python bash scripts/update.sh             # боевое обновление
```

---

## Чек-лист приёмки 7.5a

- [ ] пароль root сменён, ключевой вход работает;
- [ ] `ssh -o PubkeyAuthentication=no` → `Permission denied (publickey)` и для `root`, и для `vibebunker`;
- [ ] `ufw status` → active, открыты только 22/80/443;
- [ ] `fail2ban-client status sshd` → джейл активен;
- [ ] `nginx -t` → syntax is ok;
- [ ] `systemctl is-enabled messenger` → enabled, `curl 127.0.0.1:8000/health` → ok;
- [ ] `https://vibebunker.ru/health` отвечает, замок валиден, `certbot renew --dry-run` зелёный;
- [ ] первый зарегистрированный = админ/creator, сообщения и вложения ходят, WS живой;
- [ ] `scripts/backup.py` отработал вручную и стоит в cron.
