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

## UI-полировка и присутствие (Phase 7.8)

### Шапка — остров, внутри неё остров «ава + ник»

| ТЗ | Как сделано |
|---|---|
| Шапка = остров liquid glass на всех экранах | `.chat-header` — радиус 16px, `backdrop-filter`, цвет из токена `header_color` (`--header-island-color`); верхняя шапка приложения красится тем же токеном, стекло сохраняется |
| Вложенный остров «ава + ник» | `#chatPeerIsland` (`.chat-peer-island`) — пилюля под стеклом внутри шапки; на десктопе и на мобиле (390×844: ава 28px, прижата к стрелке «Назад», `gap` 4px — без дыр по центру) |
| Цвет ника | токен `name_color` (`--name-color`) красит `#chatHeaderName` и `#otherProfileName`; по умолчанию тумблер `name_color_auto` = **вкл** → цвет считается YIQ-автоконтрастом от `panel` (тёмная тема → `#fff`, светлая → `#111`), выкл → берётся сам токен |
| Тёмная тема без чёрного текста | производные токены `--header-text`, `--header-island-text`, `--name-color` считаются `contrastText()`; те же производные ставит `applyThemeToElement()` для **чужой** темы в модалке профиля — чужой тёмный пресет не оставляет чёрный ник |
| Баннер собеседника в шапке | тумблер `peer_banner_header` (per-user, лежит в `theme_json.toggles`): вкл — `#chatHeaderBanner` под стеклом вложенного острова, выкл — просто цвет. У канала берётся его собственный баннер |

### Присутствие (online / last_seen)

`online` — факт живого WS-подключения (`app.state.connections`), в БД не хранится.
`last_seen` — колонка `users.last_seen`: пишется на подключении, на отключении и раз в минуту
фоновой задачей `presence_heartbeat()` для всех, кто держит соединение.

| Событие | Что происходит |
|---|---|
| WS-коннект | `ensure_presence_heartbeat()` + `touch_last_seen()` + рассылка `{"type":"presence","user_id","online":true,"last_seen"}` |
| WS-дисконнект | удаление из `connections` + `touch_last_seen()` + `presence` с `online:false` |
| Раз в минуту | `last_seen` обновляется у всех онлайн (heartbeat) |

Клиент: `presenceMap` → `renderPresenceRings()` (граница авы: зелёная `presence_online` /
серая `presence_offline`), `renderPresenceIsland()` — островок «был(а) в сети сегодня в HH:MM»
(«вчера», «5 сентября в HH:MM», «давно»), скрыт когда собеседник онлайн или печатает
(typing приоритетнее). Цвет текста островка — токен `presence_text`.
Данные: `GET /api/users` и `GET /api/user/{id}/profile` отдают `online` + `last_seen`,
плюс фоновый опрос раз в 45 с.

### Мелочи

| ТЗ | Как сделано |
|---|---|
| Трэш в контактах | `.curtain-actions` — `justify-content: flex-end`, `padding-right: 10px`, ширина по иконке (44px), сдвиг строки `-64px`; `.user-info { flex:1; min-width:0 }` — ник не наезжает |
| Мобила: подтверждение удаления | `#deleteChatConfirm` вынесен из `.chat-area` (на мобиле она уведена за экран `translateX(100%)`) на верхний уровень: `position: fixed` + затемнение `#deleteChatBackdrop`; тап по затемнению, круглая кнопка и Escape закрывают |
| Кнопки закрытия | единый `.island-close` (круглый островок 34px с SVG-крестом) в редакторе тем, профиле, чужом профиле, создании группы, участниках, удалении аккаунта, модалке картинки и в админ-модалках (`#warnCloseBtn`, `#banCloseBtn`) |
| Канал | объявления по центру (`.system-announcement`), md-рендер с переносами строк; `\n → <br>` теперь **только вне** ` ```блоков``` ` (внутри `<pre>` работают настоящие переводы строк) |

### 7.8-fix: аватары, desktop-остров, reload, скоуп токена, объявления в теме

| # | Что сделано | Где |
|---|---|---|
| [1] | Аватар собеседника рендерится **безусловно**: в контактах (`renderUserAvatars()` — без `if (avatarUuid)`) и в шапке (`renderHeader()`); нет uuid → `/static/default-avatar.png`. Никакой зависимости от баннера/профиля | `renderHeader()`, `renderUserAvatars()`, `getAvatarUrl()` |
| [2] | Desktop (`min-width: 769px`): шапка чата — остров **по центру** (`width: max-content; margin: 8px auto`), padding симметричный (`0.45rem 1.2rem`), ник по центру, ава слева внутри острова, справа балансир `::after` шириной в аву (32px) — концы острова равноудалены от ника. Кнопки действий — в правом отсеке. Мобила (`max-width: 768px`) не изменилась | `@media (min-width: 769px)` в `static/style.css` |
| [3] | Reload: в bootstrap-пути (`hash #c=…` / `sessionStorage`) **сразу** вызывается `renderHeader(имя, аватар[, баннер])` — шапка на месте до загрузки диалога; баннер берётся из кэша `bannerByUser`, при отсутствии строки контакта — из `/api/user/{id}/profile` | bootstrap-блок в `templates/chat.html` |
| [4] | Токен `header_color` ставится переменной **на элемент** `#chatHeader` (`style.setProperty('--header-island-color')`), а не на root/body; он исключён из `TOKEN_CSS_VARS`, поэтому цикл по токенам его не трогает. Верхняя панель мессенджера использует только `--header-color` / `--panel-color` | `applyThemeDirect()` + `header` в `static/style.css` |
| [5] | Объявления канала: фон из токена `broadcast_bg` (`--broadcast-bg`, манифест + оба пресета; тёмная `#2d3436`), текст — YIQ-автоконтраст `--broadcast-text` из `applyContrast()`. Центрирование и md-рендер сохранены | `THEME_TOKENS`, `THEME_PRESETS`, `applyContrast()`, `.message.system-announcement` |

### 7.8-fix2: последние штрихи

| # | Что сделано | Где |
|---|---|---|
| [1] | Крестик — `position: absolute` во всех модалках/drawer; **никаких отступов под кнопку** (убран `padding-right: 52px`), контент центрируется независимо: `.theme-drawer-header { justify-content: center }`, в админ-модалках заголовок по центру | `.island-close`, `.theme-drawer-header`, `templates/admin.html` |
| [2] | Профиль на мобиле — компактный пузырь по контенту: `height: auto; max-height: 85dvh; overflow-y: auto; width: auto; max-width: min(92%, 420px)`; растёт только вместе с информацией (не лист во весь экран) | `@media (max-width: 768px) .profile-modal-content` |
| [3] | Тумблеры без рамок: `applyEditorContrast()` больше не вешает инлайновый `border` на `.glass-toggle`; у самой кнопки `border: none; outline: none`, у дорожки — `border-radius: 999px` + `backdrop-filter` (чистая пилюля) | `applyEditorContrast()`, `.glass-toggle*` |
| [4] | Цитата в чужом пузыре — та же компоновка, что в своём: содержимое обёрнуто в `.message-body` (колонка), цитата **сверху**, текст ниже, время внизу | `appendMessage()` |
| [5] | Приватность присутствия: `users.hide_presence` (default 0), переключатель «Показывать, когда я был(а) в сети» в профиле, `POST /api/profile/presence`. Приватность **взаимная**: если скрывает кто-то один — обе стороны получают `online=false`, `last_seen=null`, `hidden=true` (и в `/api/users`, и в `/api/user/{id}/profile`, и в WS-событии `presence`). Клиент показывает островок «статус скрыт» и серую границу авы без last-seen | `presence_visible()`, `presence_flags()`, `push_presence()`, `hidePresenceToggle` |

### 7.8-fix3: геометрия мобила-профиля

Пузырь профиля (**свой** — ящик `.profile-drawer`, **чужой/канал/группа** — `.profile-modal-content`)
на мобиле (`max-width: 768px`):

| Ось | Правило |
|---|---|
| Ширина | `width: min(92vw, 420px); max-width: min(92vw, 420px)` — как на desktop, не узкая колонка по контенту |
| Высота | `height: auto; max-height: 85dvh` (fallback `85vh`) + `overflow-y: auto` — не растянута во весь экран |
| Центр | модалки: `margin: auto; align-self: center` (оверлей `.modal` на мобиле стоит `align-items/justify-content: stretch` — auto-поля перебивают stretch); ящик профиля: `position: fixed; top/left: 50%; transform: translate(-50%, -50%)` |

У профильного ящика внутри скроллится только тело (`.profile-drawer .theme-drawer-body { min-height: 0 }`),
шапка с крестиком остаётся на месте. Desktop не затронут: все правила лежат внутри media-запроса.

### Важно: инвалидация кэша браузера (7.8-fix)

PWA-сервис-воркер (`static/sw.js`) отдаёт `/static/style.css?v=…` **cache-first**, а HTML —
network-first. Пока версия в ссылке и имя кэша не меняются, браузер продолжает показывать
**старый CSS**, и любой новый стиль выглядит как «не сделано». Правило на все фазы:

1. `static/sw.js` → `const CACHE = 'vb-<фаза>'` (новое имя чистит старые кэши на `activate`);
2. `templates/chat.html` → `href="/static/style.css?v=<фаза>"`;
3. статику с `?v=` отдаём **stale-while-revalidate** (фоновая подкачка обновит кэш, даже если
   версию забыли поднять);
4. HTML-страницы уходят с `Cache-Control: no-cache, must-revalidate` — разметка несёт инлайн-JS
   чата, её устаревшая копия равна «нового функционала нет».

Серверные точки входа: `PRESENCE_HEARTBEAT_SECONDS`, `touch_last_seen()`, `push_presence()`,
`presence_heartbeat()`, `ensure_presence_heartbeat()`, токены `header_color` / `name_color` /
`presence_online` / `presence_offline` / `presence_text` и секция `toggles` манифеста
(`merge_theme_with_defaults()`, `sanitize_theme_config()`).
Фронт: `templates/chat.html` (`renderToggleRows()`, `loadPresence()`, `renderPresenceRings()`,
`peerPresenceText()`, `renderPresenceIsland()`, `refreshPeerMeta()`, `renderPeerBanner()`) и
`static/style.css` (`.chat-peer-island`, `.presence-island`, `.island-close`, `.modal-backdrop`).

## Канал объявлений «ВайбБункер» (Phase 7.6d-fix)

Канал — **не пользователь**. В `users` нет ботов: служебный `@start` удалён миграцией
(`_remove_system_user()` — чистит самого бота, его сообщения, пины, мьюты, риды). Профиль
канала живёт в `settings` (`broadcast_name`, `broadcast_bio`, `broadcast_avatar_uuid`,
`broadcast_banner_uuid`), сообщения — в `messages` с `peer_type='broadcast'`, `peer_id=0`
и `sender_id` = реальный создатель. **Creator = первый зарегистрированный** (`users.is_creator`
у `min(id)`; миграция `_ensure_creator()` назначает его и в уже существующих базах).

| Поведение | Правило |
|---|---|
| Онбординг | Автосообщений нет: канал появляется сверху **пустым**, первое объявление пишет creator сам |
| Пустой канал | У creator — плашка «Напиши первое объявление» + кнопка «Вставить шаблон» (правдивый текст про темы, жесты, режимы, группы, почту и PWA); у остальных — плашка «канал объявлений — ответы отключены» |
| Место в списке | Отдельная строка `#channelItem` над списком контактов, бейдж 📢, подпись «канал объявлений»; без «занавеса» и без стрелки действий |
| Удаление | Нельзя: `POST /api/delete-chat` с `channel=1` → **403** для всех, включая creator |
| Рендер | Объявления идут по центру (`.system-announcement`), без аватара, без цитаты и без свайп-ответа |
| Кто пишет | Только creator: `POST /api/send` с `channel=1` → **403** всем остальным, включая админов |
| Рассылка | Одна строка на всех + веерная доставка по WS (`push_to_all`); unread-бейдж считается как `type='channel', id=0` |
| Правка и удаление | Только creator: `POST /api/messages/{id}/edit` и `POST /api/delete-message` → **403** остальным; удаление рассылает `message_deleted` |
| Профиль канала | `GET /api/channel` смотрят все; `POST /api/channel`, `/api/channel/avatar`, `/api/channel/banner` — только creator (админы-не-creator — читатели) |
| Админка | Системных строк в Users нет уже потому, что бота в `users` больше нет |
| Прочее | `typing` в канал не шлётся; в поиске контактов канал не участвует |

**Markdown-подмножество в пузырях** (все чаты): рендер = `escapeHtml()` **первым шагом**, затем
`**жирный**`, `*курсив*`, `~~страйк~~`, `` `код` ``, ```блоки```, автоссылки `http(s)`
(`target="_blank" rel="noopener noreferrer"`) и переносы строк. Сырого HTML в пузыре не бывает —
XSS-безопасно по построению.

**Даты**: `parseTs()` разбирает `YYYY-MM-DD HH:MM:SS` по компонентам (в части браузеров
`new Date('YYYY-MM-DD HH:MM:SS')` даёт `Invalid Date`); сообщение без валидного `created_at`
не создаёт разделитель дат и не показывает время.

Серверные точки входа: `channel_payload()`, `get_channel_profile()`, `set_channel_fields()`,
`creator_user_id()`, `require_creator()`, `post_channel_message()`, `channel_message_rows()`,
`push_to_all()`, миграции `_migrate_channel_columns()` / `_remove_system_user()` /
`_ensure_creator()` / `_seed_channel_settings()`.
Фронт: `templates/chat.html` (`#channelItem`, `openChannel()`, `loadChannel()`,
`applyChannelMode()`, `renderMarkdown()`, `parseTs()`, `profileMode`) и `static/style.css`
(`.system-announcement`, `.channel-notice`, `.md-code`, `.md-code-block`, `.profile-channel-mode`).

## Почта и коды подтверждения (Phase R7)

Смена пароля, смена почты и верификация адреса подтверждаются 6-значным кодом из письма.

**Настройка** (`SMTP_*` в `.env` или в окружении):

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=noreply@example.com
SMTP_PASS=...
SMTP_FROM="VibeBunker <noreply@example.com>"
SMTP_STARTTLS=yes     # yes|no
```

Бэкенд выбирается автоматически:

| `SMTP_HOST` | режим | поведение |
|---|---|---|
| задан | `smtp` | письмо уходит через `smtplib` (STARTTLS, при `SMTP_USER` — логин) |
| пуст, `APP_MODE=dev` | `console` | письмо не нужно: код виден в `data/logs/app.log` строкой `EMAIL CODE user_id=… purpose=… code=NNNNNN` |
| пуст, `APP_MODE=prod` | `none` | эндпоинты кодов отдают **503** «Почта не настроена: задайте SMTP_HOST» |

Текущий бэкенд виден в `GET /api/profile` в поле `mail_backend`.

**Политика кодов:** 6 цифр из `secrets` (не `random`), TTL 10 минут, 5 попыток ввода
(после пятой код блокируется), отправка — не чаще одного кода в 60 секунд и не больше пяти
в час на пользователя (429 + `Retry-After`). В БД — только `sha256(code)` в таблице
`email_codes` (`user_id`, `purpose`, `target`, `code_hash`, `created_at`, `expires_at`,
`attempts`, `used`): plaintext кода не хранится и в `prod` не логируется.

**Флоу:**

1. **Смена пароля** — `POST /api/email/code/request` (`purpose=password_change`, `current_password`)
   → код на почту → `POST /api/profile/password` (`code`, новый пароль). Плюс `session_epoch++`:
   остальные устройства разлогиниваются (R6).
2. **Смена почты** — `POST /api/email/code/request` (`purpose=email_change`, `new_email`,
   `current_password`) → код уходит на **новый** адрес → `POST /api/profile/email`
   (`email`, `code`): адрес обновлён и сразу помечен верифицированным.
3. **Верификация почты** — `POST /api/email/code/request` (`purpose=verify`) →
   `POST /api/email/code/confirm` (`purpose=verify`, `code`) → `users.email_verified=1`,
   в профиле появляется бейдж «✓ подтверждена». Регистрация верификацией **не** блокируется
   (гейт — инвайты), верификация опциональна.

## Security-аудит R6 (OWASP Top 10)

Аудит по OWASP Top 10 (2021). Статусы: **OK** — так и было, **FIXED** — усилено в фазе R6,
**N/A** — неприменимо. Улики — строки в коде/шаблонах на момент коммита фазы.

| Категория | Статус | Улики / что сделано |
|---|---|---|
| **A01 Broken Access Control** (IDOR) | FIXED | Проверки владения в каждом `/api/*`: правка — только автор `edit_message` (`Only author can edit`), удаление — участник/админ (`Not authorized to delete this message`, `Only admins can delete messages for all`), вложения — только участник диалога или группы (`GET /api/attachment/{id}` и `/info`, `Not a group member` / `Forbidden`), `blocks`/`pins` — только свои (`WHERE blocker_id = ? AND blocked_id = ?`, `DELETE FROM pins WHERE user_id = ?`), админ-эндпоинты — 15 вызовов `require_admin()` (строка 2747). Selftest: `t`, `u`, `w`. |
| **A02 Cryptographic Failures** | OK | Секреты вне логов (R4: `grep -icE "session=\|secret_key=" data/logs/*.log` → 0); ключ сессии — `HttpOnly`; `SameSite=Lax` по умолчанию, `None; Secure` **только** через `SESSION_SAME_SITE=none SESSION_SECURE=1` (строка 447); пароли — PBKDF2-HMAC-SHA256, 100 000 итераций, соль на пользователя (`hash_password`). |
| **A03 Injection** | FIXED | SQL — только параметризованный: `grep -nE "execute\(\s*f\"\|execute\([^)]*\+"` по `main.py` и `scripts/*.py` даёт 2 DDL-строки с константами, теперь с whitelist-проверкой имени колонки (`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name)`, строки 854, 884). `scripts/*` не делают shell-вызовов с пользовательскими данными (`subprocess` — только в `selftest.py`, с фиксированными аргументами). |
| **A04 Insecure Design** | OK | Rate-limit логина (5 попыток / 10 мин → 429), лимиты размера вложений (`MAX_UPLOAD_BYTES`, `THEME_IMAGE_MAX_BYTES`), удаление брошенных `.part`, invite-only регистрация после первого пользователя. |
| **A05 Security Misconfiguration** | FIXED | Заголовки на каждом ответе: `X-Content-Type-Options: nosniff` (стр. 666), `Referrer-Policy: no-referrer` (672), `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=(), usb=(), …` (674), CSP (678); `FastAPI(debug=(APP_MODE == "dev"))` (449) — трассировки клиенту только в dev; `/api/admin/debug-boom` в `prod` отдаёт 404. |
| **A06 Vulnerable Components** | OK | Все зависимости запинены: `requirements.txt` — 19 пакетов, все с `==` (fastapi 0.109.2, starlette 0.36.3, uvicorn 0.27.0, Jinja2 3.1.3 и т.д.). |
| **A07 Identification & Auth Failures** | FIXED | Смена пароля требует текущий (`Current password is incorrect`); логин пересоздаёт сессию — `request.session.clear()` перед записью (стр. 1465) — защита от session fixation; смена пароля поднимает `users.session_epoch` (3239) и рвёт сессии на других устройствах: `get_current_user` сверяет `session["epoch"]` с БД (1277), `смена пароля: user_id=…, прочие сессии сброшены (epoch=N)` в app.log. |
| **A08 Software & Data Integrity** | OK | `update.sh` с PRE/POST-чеком и автооткатом (R3), online-бэкап `backup.py` (R2), целостность БД `integrity_check ok` при старте, WAL. |
| **A09 Logging & Monitoring Failures** | OK | `data/logs/app.log` + `error.log` с ротацией, пульс-метрики, админ-аудит, необработанные исключения → `error.log` с traceback (R4). |
| **A10 SSRF** | N/A | Приложение не делает исходящих HTTP-запросов по пользовательским URL: внешних `fetch`/`requests` в коде нет. |

### CSRF (Phase R6)

При `SESSION_SAME_SITE=none` (работа в iframe) `SameSite` перестаёт защищать от CSRF — поэтому
включён double-submit:

- сервер выдаёт cookie `csrftoken` (`Path=/`, `SameSite`/`Secure` — как у сессии, **не** HttpOnly:
  фронтенд читает своё же значение) — `CSRFMiddleware`, `main.py:554`;
- на каждом **изменяющем** запросе (POST/PUT/PATCH/DELETE) значение должно прийти
  в заголовке `X-CSRF-Token` либо в поле формы `csrf_token` (HTML-формы и multipart-аплоады);
- сравнение — `hmac.compare_digest` (стр. 607), без токена или при несовпадении → **403**
  `{"detail": "CSRF token missing or invalid"}` + запись в лог `CSRF отклонён: …`;
- мидлварь — чистый ASGI (не `BaseHTTPMiddleware`): тело буферизуется и отдаётся приложению
  целиком, иначе эндпоинты получали бы пустой form; тела больше `MAX_UPLOAD_BYTES + 1 МБ` → 413;
- фронтенд: один патч `window.fetch` в `templates/chat.html` и `templates/admin.html`
  добавляет заголовок ко всем изменяющим запросам; HTML-формы `/login` и `/register`
  несут `{{ request.state.csrf_token }}` в скрытом поле.

### XSS-аудит (Phase R6)

Пользовательский текст попадает в DOM только через `escapeHtml`:

- `templates/chat.html` — пузырь сообщения собирается из экранированных частей:
  `${escapeHtml(msg.text)}`, `${escapeHtml(msg.sender_name)}`, цитата
  `${escapeHtml(msg.reply_to_name)}` / `${escapeHtml(snippet)}`, вложения
  `alt="${escapeHtml(att.orig_name)}"`, списки — `${escapeHtml(g.name)}`, `${escapeHtml(m.name)}`;
- `templates/admin.html` — добавлен `escapeHtml` и применён ко всем вставкам (инвайты
  `used_by_username`, аудит `actor`/`action`/`target`/`detail`, пульс `ts`/`kind`/`detail`);
- Jinja-шаблоны рендерятся с автоэкранированием, `| safe` в проекте не используется;
- CSP запрещает чужие скрипты: `script-src 'self' 'unsafe-inline'`.

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

## Конфигурация: .env, режимы, стартовые проверки

Приложение стартует одной командой `python main.py` (или `uvicorn main:app`): настройки
берутся из `.env` в корне репозитория — его читает `scripts/load_env.py` (stdlib, без зависимостей).

```bash
cp .env.example .env && nano .env
python main.py
```

Правила загрузки:
- формат `KEY=VALUE`, `#` — комментарий (строчный и хвостовой), значения можно в кавычках;
- **уже заданные переменные окружения не перезаписываются**: `SECRET_KEY=… python main.py` важнее файла;
- нет `.env` — не ошибка, работают значения по умолчанию.

Режимы (`APP_MODE`, по умолчанию `dev`):
- `dev` — слабый или отсутствующий `SECRET_KEY` разрешён, в `app.log` уходит warning;
- `prod` — `SECRET_KEY` обязан быть не короче 32 символов (и не `localdev`), иначе error в лог и **exit 1**.

Стартовые проверки (провал — exit 1 с понятным сообщением):
- `DATA_DIR` существует/создаётся и доступен для записи (пробный файл создаётся и удаляется);
- в `prod` — проверка `SECRET_KEY`.

Что получилось в итоге, видно в логе старта:
```
dev-режим: SECRET_KEY задан (64 символов)
сессия: cookie same_site=none secure=True; X-Frame-Options=none
старт приложения: DATA_DIR=./data, PORT=8000
journal_mode = wal
integrity_check ok
```

## Режим сессионной cookie и встраивание

По умолчанию cookie сессии — `SameSite=Lax` без `Secure`, а ответы содержат `X-Frame-Options: DENY`
(приложение открывается напрямую, не во фрейме). Если сервис открывают внутри iframe на другом
домене (например, предпросмотр в браузере), браузер такую cookie не отправит — после логина
`/chat` будет снова кидать на `/`. Для этого случая есть env-переключатели:

```bash
SESSION_SAME_SITE=none SESSION_SECURE=1 FRAME_OPTIONS=none python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- `SESSION_SAME_SITE` — `lax` (default) | `strict` | `none`;
- `SESSION_SECURE=1` — добавляет флаг `Secure` (обязателен для `SameSite=None`, нужен HTTPS);
- `FRAME_OPTIONS` — `deny` (default) | `sameorigin` | `none` (заголовок не отправляется).

Режим виден в логе при старте: `сессия: cookie same_site=... secure=...; X-Frame-Options=...`.

## УСТАВ (правила приёмки фаз)

1. Каждая фаза живёт в своей ветке; `main` не трогаем до явного решения о мерже.
2. Каждая фаза заканчивается коммитом с маркером `- phase <N> complete` (хотфиксы — `- <N>-fix complete`).
3. Маркер фазы дублируется в конец этого README — история фаз читается снизу вверх.
4. Меняем только то, что нужно фазе: никаких попутных переездов, переименований и «улучшений мимоходом».
5. `.gitignore` не редактируется без отдельной задачи.
6. Конфигурация — только через env (`SECRET_KEY`, `PORT`, `DATA_DIR`, `MAX_UPLOAD_BYTES`, `FIRST_USER_ADMIN`); значения по умолчанию в коде не подменяются.
7. `DATA_DIR` — единственный источник истины для персистентного состояния: БД и вложения выводятся из него, тесты указывают свой временный каталог.
8. Поведение по умолчанию не меняется: всё новое либо за флагом, либо обратно совместимо.
9. Логика прав (`require_admin`, матрица кика, доступ к вложениям) проверяется на сервере, а не в клиенте.
10. Фаза без воспроизводимой проверки считается незакрытой: скрипт, отчёт, маркер.
11. Фаза принимается **ТОЛЬКО** с зелёным `scripts/selftest.py`; последние 20 строк его вывода вкладываются в отчёт о фазе.
    Все соединения с SQLite создаются только через `get_conn()`; вложения пишутся в `*.part` и появляются через `os.replace`;
    `data/` целиком в `.gitignore`, поэтому `-wal`, `-shm` и `*.part` в git не попадают.
12. События приложения идут через `logging`, а не `print`: `data/logs/app.log` (INFO, ротация 5 МБ × 5) и
    `data/logs/error.log` (только ERROR+, своя ротация), формат `время | level | модуль | сообщение`.
    В логи не попадают cookie, `SECRET_KEY` и пароли — проверяется grep'ом по лог-вызовам:
    `grep -nE "(app_logger|error_logger|admin_logger|_login_logger|_ws_logger|_upload_logger)\.(info|warning|error|exception)" main.py | grep -iE "cookie|secret|password|token"`.

```bash
python scripts/selftest.py   # exit 0 только если все сценарии OK
```

Селftest поднимает отдельный инстанс на `PORT=8099` во временном `DATA_DIR`, удаляет его после прогона
и не трогает рабочий сервер; прогон занимает меньше 30 секунд.

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

Edit `/etc/messenger/env` (или просто `cp .env.example .env` в корне репозитория —
приложение само прочитает его при старте, см. ниже):
```
APP_MODE=prod
SECRET_KEY=<generate-with-python-c-secrets.token_hex(32)>
FIRST_USER_ADMIN=1   # Set to 1 ONLY for initial setup, then 0
MAX_UPLOAD_BYTES=5242880
THEME_IMAGE_MAX_BYTES=5242880
PORT=8000
DATA_DIR=/var/lib/messenger/data
BACKUP_DIR=/var/lib/messenger/backups
RETAIN_COUNT=5
UPDATE_BRANCH=main
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

Обновление — только через `scripts/update.sh`: он сначала прогоняет selftest (на сломанном
состоянии обновляться запрещено), снимает бэкап БД, делает `git fetch` + `reset --hard origin/<ветка>`,
перезапускает сервис (systemd или dev-режим) и снова прогоняет `/health` + selftest.
Зелёный постинчек — `data/last_good_hash` обновляется; красный — автоматический откат:
код на предыдущий HEAD, БД из бэкапа, рестарт.

```bash
cd /opt/messenger
sudo DATA_DIR=/var/lib/messenger/data BACKUP_DIR=/var/lib/messenger/backups \
     UPDATE_BRANCH=main ./scripts/update.sh
sudo DATA_DIR=/var/lib/messenger/data ./scripts/update.sh --dry-run   # только план, без изменений
```

Exit-коды: `0` — UPDATE OK, `1` — выполнен откат, `2` — pre-check красный,
`3` — не снялся бэкап, `4` — не удался fetch/reset.

Ручной вариант (без проверок, на свой страх и риск):

```bash
cd /opt/messenger
sudo git pull
sudo systemctl restart messenger
```

## Backup

Бэкап снимается онлайн, без остановки сервиса: `scripts/backup.sh` — тонкий шим
над `scripts/backup.py`, который копирует БД через SQLite Online Backup API
(`sqlite3.Connection.backup()`, корректно работает с WAL) и обязательно проверяет
**копию** через `PRAGMA integrity_check`. Битую копию скрипт бэкапом не считает:
печатает `ERROR: копия ... битая` и завершается ненулевым кодом.

Проверить уже существующую копию (например, перед восстановлением):

```bash
python3 scripts/backup.py --verify /var/lib/messenger/backups/messenger-YYYYMMDD_HHMMSS.db
# exit 0 — копия целая; exit 3 — битая
```

### Manual backup

```bash
sudo -u messenger /opt/messenger/scripts/backup.sh
```

### Automated backup (cron)

Add to crontab (`sudo crontab -e`):
```
0 2 * * * DATA_DIR=/var/lib/messenger/data BACKUP_DIR=/var/lib/messenger/backups RETAIN_COUNT=5 /opt/messenger/scripts/backup.sh
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

- 7.4a-fix complete

- 7.4a-fix2 complete
- 7.4a-fix3 complete
- 7.4a-fix4 complete
- phase 7.4b complete
- micro statusbar complete
- micro a11y complete
- micro a11y2 complete

- phase R1 complete

- phase R2 complete

- phase R3 complete

- phase R4 complete

- phase R5 complete

- phase R6 complete

- phase R7 complete

- phase 7.6d complete

- 7.6d-fix complete
- phase 7.8 complete
- 7.8-fix complete
- 7.8-fix complete
- 7.8-fix2 complete
- 7.8-fix3 complete
