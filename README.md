# Codex Limits Monitor

Telegram-бот на Python 3.11+ для независимого мониторинга 5-часовых и недельных лимитов нескольких Codex-аккаунтов. Авторизация выполняется официальным Device Code-потоком Codex App Server. Пароли, cookie и OAuth-токены не проходят через Telegram и не сохраняются в SQLite.

## Архитектура

- `codex_monitor/appserver.py` — построчный JSON-RPC-клиент для `codex app-server --stdio`, Device Code-авторизация и разбор лимитов.
- `codex_monitor/db.py` — асинхронная SQLite-схема: пользователи, аккаунты, настройки, состояния окон и дедуплицированные события.
- `codex_monitor/detector.py` — независимый от Telegram детектор фактических сбросов.
- `codex_monitor/monitor.py` — фоновый опрос с ограничением параллелизма, повторами и уведомлениями.
- `codex_monitor/ui.py` — одно редактируемое главное сообщение, inline-навигация и временный текстовый ввод.
- `codex_monitor/__main__.py` — запуск, логирование и корректная остановка.
- `tests/` — искусственные сценарии лимитов, перезапуска, дедупликации и базовых элементов интерфейса.

Каждому аккаунту назначается случайный внутренний ключ и отдельный каталог в `ACCOUNTS_STORAGE_PATH`. Только Codex CLI хранит там свою авторизацию. Опрос создаёт короткоживущий App Server-процесс и ограничен `MONITOR_MAX_CONCURRENCY`, поэтому на малом VPS не остаётся постоянного тяжёлого процесса на каждый аккаунт. Процесс авторизации живёт только до успеха, ошибки, отмены или таймаута.

## Как определяется сброс

Окна определяются по `windowDurationMins`, а не по позиции `primary`/`secondary`:

- 240–360 минут — 5-часовое окно;
- 9000–11000 минут — недельное окно;
- неизвестное или неоднозначное окно не подменяется другим.

Первое достоверное показание становится базовым. Кандидат на сброс появляется, когда использование уменьшилось минимум на 20 процентных пунктов и восстановилось не менее 40% ранее израсходованного лимита. Второй запрос должен подтвердить близкое значение с допуском 3 пункта. Сброс считается плановым, если подтверждение произошло в пределах 15 минут от прежнего `resetsAt`; иначе — досрочным.

Кандидат и последнее достоверное состояние сохраняются в SQLite. Событие имеет уникальный отпечаток, поэтому рестарт не создаёт повторное уведомление. Ошибка, отсутствующее окно или одиночный аномальный ответ событием не считаются.

## Запуск в Windows

Требуются:

1. Python 3.11 или новее (`py -3 --version`).
2. Node.js/npm для установки официального Codex CLI.
3. Codex CLI в `PATH`:

```powershell
npm install -g @openai/codex@latest
codex --version
```

4. Токен Telegram-бота. Создайте бота через `@BotFather`, выполните `/newbot` и скопируйте выданный токен.

Первый запуск:

```powershell
start.bat --check
notepad .env
start.bat
```

`start.bat` создаёт `.venv`, устанавливает зависимости, при отсутствии копирует `.env.example` в `.env` и запускает приложение. В `.env` обязательно заполнить только:

```dotenv
TELEGRAM_BOT_TOKEN=полученный_у_BotFather_токен
```

Не отправляйте `.env` в Git. Команда `start.bat --check` не запускает Telegram-бота и не выполняет авторизацию: она проверяет Python, Codex CLI и создание схемы SQLite.

## Конфигурация

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `CHECK_INTERVAL_SECONDS` | `300` | Интервал фоновой проверки |
| `RESET_CONFIRM_DELAY_SECONDS` | `5` | Пауза перед подтверждающим запросом |
| `DATABASE_PATH` | `./data/codex_monitor.db` | SQLite |
| `ACCOUNTS_STORAGE_PATH` | `./accounts` | Изолированные `CODEX_HOME` |
| `LOG_PATH` | `./logs/codex_monitor.log` | Ротируемый лог |
| `CODEX_EXECUTABLE` | `codex` | Команда или полный путь к CLI |
| `APP_SERVER_REQUEST_TIMEOUT_SECONDS` | `30` | Таймаут JSON-RPC-запроса |
| `AUTH_TIMEOUT_SECONDS` | `900` | Срок ожидания Device Code |
| `MONITOR_MAX_CONCURRENCY` | `2` | Максимум одновременных App Server-процессов |
| `MONITOR_RETRY_ATTEMPTS` | `3` | Повторы временной ошибки |
| `ERROR_NOTIFICATION_THRESHOLD` | `3` | Серия неудачных циклов перед уведомлением |

## Linux VPS и systemd

Пример для Ubuntu/Debian. Выполняйте команды пользователем с `sudo`:

Репозиторий приватный: команда `git clone` ниже требует заранее настроенный deploy key (ключ развёртывания с доступом только на чтение) либо GitHub credential helper (безопасное хранилище учётных данных GitHub). Не помещайте GitHub-токен в URL, `.env` или shell history (историю введённых команд). Без GitHub-доступа доставляйте проверенный commit через `git archive`/`scp`; в таком развёртывании обновление также выполняется новым архивом, а не `git pull`.

```bash
sudo apt update
sudo apt install -y python3 python3-venv nodejs npm git
sudo npm install -g @openai/codex@latest
codex --version

sudo useradd --system --home /opt/codex-limits-monitor --shell /usr/sbin/nologin codexmonitor
sudo git clone https://github.com/wpoohbtw/CodexCheckBot.git /opt/codex-limits-monitor
sudo python3 -m venv /opt/codex-limits-monitor/.venv
sudo /opt/codex-limits-monitor/.venv/bin/pip install -r /opt/codex-limits-monitor/requirements.txt
sudo cp /opt/codex-limits-monitor/.env.example /opt/codex-limits-monitor/.env
sudo install -d -o codexmonitor -g codexmonitor -m 700 \
  /opt/codex-limits-monitor/data \
  /opt/codex-limits-monitor/accounts \
  /opt/codex-limits-monitor/logs
sudo chown -R codexmonitor:codexmonitor /opt/codex-limits-monitor
sudo chmod 600 /opt/codex-limits-monitor/.env
sudo nano /opt/codex-limits-monitor/.env
```

Если `codex` установлен не в системный `PATH`, задайте абсолютный `CODEX_EXECUTABLE` в `.env`.

Проверка и служба:

```bash
cd /opt/codex-limits-monitor
sudo -u codexmonitor .venv/bin/python -m codex_monitor --check
sudo cp deploy/codex-limits-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-limits-monitor
sudo systemctl status codex-limits-monitor
sudo journalctl -u codex-limits-monitor -f
```

Обновление:

```bash
sudo systemctl stop codex-limits-monitor
cd /opt/codex-limits-monitor
sudo -u codexmonitor git pull --ff-only
sudo -u codexmonitor .venv/bin/pip install -r requirements.txt
sudo systemctl start codex-limits-monitor
```

Перед обновлением рекомендуется копировать `data/codex_monitor.db`, `.env` и каталог `accounts/` в защищённое место. Для отката верните прежний commit и резервную копию данных, затем перезапустите службу.

## Проверки

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q codex_monitor tests
.venv\Scripts\python.exe -m scripts.check_appserver
```

Последняя команда использует новый временный пустой `CODEX_HOME`, выполняет только `initialize`, `initialized` и `account/read`, а затем удаляет каталог. Она не запускает модель, не расходует генерационные квоты и не использует сохранённый аккаунт.

## Ограничения

- Реальную Device Code-авторизацию и реальные ответы лимитов можно проверить только с согласия владельца ChatGPT-аккаунта. Тесты не имитируют такой вход как успешно завершённый.
- App Server остаётся развивающимся интерфейсом. Реализация проверена по схеме установленного Codex CLI и использует методы `account/login/start`, `account/login/completed`, `account/read`, `account/rateLimits/read`, `account/login/cancel`, `account/logout`. После обновления CLI рекомендуется повторить тесты и `python -m scripts.check_appserver`.
- При изменении продолжительности окон за заданные безопасные диапазоны бот покажет отсутствие достоверных данных вместо ошибочной подмены окна.
- Один Telegram-токен должен обслуживаться только одним запущенным экземпляром polling-бота.

Актуальные первичные источники протокола: [исходный код App Server Protocol](https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol) и [реализация методов аккаунта](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/account.rs).
