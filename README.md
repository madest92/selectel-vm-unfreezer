# selectel-vm-unfreezer

*[Русская версия ниже / Russian version below](#русская-версия)*

Automatically starts ("unshelves") [Selectel Cloud](https://selectel.ru/?ref_code=0d4f1f4484) servers as soon as Selectel freezes them (status `SHELVED_OFFLOADED`), polling one or more regions on an interval. Also exposes server state as [Prometheus metrics](#metrics).

## Installation

Requires Python 3.12+ and a Selectel service user with the `compute.admin` and `vpc.viewer` roles on the target project.

Create one in the [Selectel control panel](https://my.selectel.ru/iam/service-users?ref_code=0d4f1f4484): **IAM → Service users → Add service user**, set a username/password (these become `SELECTEL_USER`/`SELECTEL_PASS`), then grant both roles under the **Projects** access scope.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Every setting is a CLI flag or an environment variable (CLI wins). `SELECTEL_*` env vars keep that prefix even though the flags don't, to avoid clashing with vars like `USER` that your shell almost certainly already has.

| CLI flag                       | Environment variable       | Default      | Description                                                                      |
|--------------------------------|----------------------------|--------------|----------------------------------------------------------------------------------|
| `--host`                       | `HOST`                     | `0.0.0.0`    | Address the metrics HTTP server binds to                                         |
| `--port`                       | `PORT`                     | `8000`       | Port the metrics HTTP server listens on                                          |
| `--account`                    | `SELECTEL_ACCOUNT`         | *(required)* | Selectel account (domain) name/ID                                                |
| `--project`                    | `SELECTEL_PROJECT`         | *(required)* | Selectel project name                                                            |
| `--user`                       | `SELECTEL_USER`            | *(required)* | Selectel service user name                                                       |
| `--password`                   | `SELECTEL_PASS`            | *(required)* | Selectel service user password (prefer the env var, see note below)              |
| `--region` (repeatable)        | `SELECTEL_REGIONS`         | `ru-7`       | Region(s) to monitor, e.g. `ru-7`, `ru-3`; env var is comma-separated            |
| `--ignore-server` (repeatable) | `SELECTEL_IGNORED_SERVERS` | *(none)*     | Server name(s) or ID(s) to never start automatically, optionally `REGION/SERVER` |
| `--metric-update-interval`     | `METRIC_UPDATE_INTERVAL`   | `10`         | Seconds between status refresh cycles                                            |
| `--log-level`                  | `LOG_LEVEL`                | `info`       | Logging verbosity: `debug`, `info`, `warning`, `error`                           |

> Prefer `SELECTEL_PASS` over `--password` — CLI args are visible to other users via `ps aux`.

### Ignoring servers

An ignore-list entry is a server name or ID. A bare entry matches in every polled region; prefixing it with a region narrows it to that one region, which is what you want when the same name exists in several regions:

```bash
--ignore-server staging-db        # every region it appears in
--ignore-server ru-7/staging-db   # only the ru-7 one
--ignore-server b2c3d4e5-6f70     # by ID, unambiguous by construction
```

Only the first slash separates region from server, so `ru-7/team/db` scopes `team/db` to `ru-7`. An entry that names a region you don't monitor can never match, so it is reported as a warning at startup; a malformed entry such as `ru-7/` is rejected outright.

## Usage

```bash
python main.py \
  --account my-account --project my-project \
  --user server-monitor --password "$SELECTEL_PASS" \
  --region ru-7 --region ru-3 \
  --ignore-server staging-db
```

Or via Docker, using the environment variables instead:

```bash
docker build -t selectel-vm-unfreezer .
docker run -d -p 8000:8000 \
  -e SELECTEL_ACCOUNT=my-account -e SELECTEL_PROJECT=my-project \
  -e SELECTEL_USER=server-monitor -e SELECTEL_PASS=secret \
  -e SELECTEL_REGIONS=ru-7,ru-3 \
  -e SELECTEL_IGNORED_SERVERS=staging-db,test-vm \
  selectel-vm-unfreezer
```

Full flag list: `python main.py --help`.

## Metrics

Served at `http://localhost:8000/metrics`. Both gauges carry the same `server_name`, `server_id` and `region` labels, and both are exported for every server found in the polled regions:

| Metric                    | Values                       | Description                                         |
|---------------------------|------------------------------|-----------------------------------------------------|
| `selectel_server_up`      | `1` = running, `0` = frozen  | Status as reported by Selectel                      |
| `selectel_server_ignored` | `1` = ignored, `0` = managed | Whether the server is excluded from automatic start |

Ignored servers are still polled and still reported — they are simply never unshelved, so a frozen one stays at `selectel_server_up 0` for as long as it is ignored. Keeping the ignore list in its own metric lets alerts tell "frozen and nobody is going to fix it" apart from "frozen on purpose":

```promql
# Frozen servers that should have been started automatically
selectel_server_up == 0 and selectel_server_ignored == 0

# Servers currently on the ignore list
selectel_server_ignored == 1
```

The label sets are identical, so these joins need no `on(...)` clause.

---

## Русская версия

Автоматически запускает («unshelve») серверы [Selectel Cloud](https://selectel.ru/?ref_code=0d4f1f4484), как только Selectel их заморозит (статус `SHELVED_OFFLOADED`), периодически опрашивая один или несколько регионов. Дополнительно отдаёт состояние серверов в виде [метрик Prometheus](#метрики).

### Установка

Нужны Python 3.12+ и сервисный пользователь Selectel с ролями `compute.admin` и `vpc.viewer` на нужном проекте.

Создать пользователя можно в [панели управления Selectel](https://my.selectel.ru/iam/service-users?ref_code=0d4f1f4484): **IAM → Сервисные пользователи → Добавить сервисного пользователя**, задать логин и пароль (это и есть `SELECTEL_USER`/`SELECTEL_PASS`), затем выдать обе роли в области доступа **Проекты**.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Конфигурация

Любой параметр — это флаг CLI или переменная окружения (флаг важнее). Переменные `SELECTEL_*` сохраняют этот префикс, хотя у флагов его нет — чтобы не пересекаться с переменными вроде `USER`, которая почти всегда уже задана в шелле.

| Флаг CLI                            | Переменная окружения       | По умолчанию   | Описание                                                                                |
|-------------------------------------|----------------------------|----------------|-----------------------------------------------------------------------------------------|
| `--host`                            | `HOST`                     | `0.0.0.0`      | Адрес, на котором поднимается HTTP-сервер метрик                                        |
| `--port`                            | `PORT`                     | `8000`         | Порт HTTP-сервера метрик                                                                |
| `--account`                         | `SELECTEL_ACCOUNT`         | *(обязателен)* | Аккаунт (домен) Selectel                                                                |
| `--project`                         | `SELECTEL_PROJECT`         | *(обязателен)* | Имя проекта Selectel                                                                    |
| `--user`                            | `SELECTEL_USER`            | *(обязателен)* | Имя сервисного пользователя Selectel                                                    |
| `--password`                        | `SELECTEL_PASS`            | *(обязателен)* | Пароль сервисного пользователя (лучше через переменную окружения, см. ниже)             |
| `--region` (можно повторять)        | `SELECTEL_REGIONS`         | `ru-7`         | Регион(ы) для мониторинга, например `ru-7`, `ru-3`; в env — список через запятую        |
| `--ignore-server` (можно повторять) | `SELECTEL_IGNORED_SERVERS` | *(нет)*        | Имена или ID серверов, которые не запускать автоматически, можно с префиксом `REGION/`  |
| `--metric-update-interval`          | `METRIC_UPDATE_INTERVAL`   | `10`           | Интервал между циклами опроса, в секундах                                               |
| `--log-level`                       | `LOG_LEVEL`                | `info`         | Уровень логирования: `debug`, `info`, `warning`, `error`                                |

> Используйте `SELECTEL_PASS` вместо `--password` — аргументы командной строки видны другим пользователям через `ps aux`.

#### Игнорирование серверов

Элемент ignore-списка — это имя или ID сервера. Без префикса он совпадает во всех опрашиваемых регионах; префикс с регионом сужает его до одного региона — это то, что нужно, когда одно и то же имя встречается в нескольких регионах:

```bash
--ignore-server staging-db        # во всех регионах, где он есть
--ignore-server ru-7/staging-db   # только тот, что в ru-7
--ignore-server b2c3d4e5-6f70     # по ID, всегда однозначно
```

Регион от сервера отделяет только первый слеш, поэтому `ru-7/team/db` привязывает `team/db` к `ru-7`. Элемент, указывающий на неопрашиваемый регион, совпасть не может — о таком при старте пишется предупреждение; некорректный элемент вроде `ru-7/` отвергается сразу.

### Использование

```bash
python main.py \
  --account my-account --project my-project \
  --user server-monitor --password "$SELECTEL_PASS" \
  --region ru-7 --region ru-3 \
  --ignore-server staging-db
```

Либо через Docker, используя переменные окружения:

```bash
docker build -t selectel-vm-unfreezer .
docker run -d -p 8000:8000 \
  -e SELECTEL_ACCOUNT=my-account -e SELECTEL_PROJECT=my-project \
  -e SELECTEL_USER=server-monitor -e SELECTEL_PASS=secret \
  -e SELECTEL_REGIONS=ru-7,ru-3 \
  -e SELECTEL_IGNORED_SERVERS=staging-db,test-vm \
  selectel-vm-unfreezer
```

Полный список параметров: `python main.py --help`.

### Метрики

Отдаются на `http://localhost:8000/metrics`. У обеих метрик одинаковые лейблы `server_name`, `server_id` и `region`, и обе экспортируются для каждого сервера в опрашиваемых регионах:

| Метрика                   | Значения                              | Описание                          |
|---------------------------|---------------------------------------|-----------------------------------|
| `selectel_server_up`      | `1` — работает, `0` — заморожен       | Статус, который сообщает Selectel |
| `selectel_server_ignored` | `1` — игнорируется, `0` — управляется | Исключён ли сервер из автозапуска |

Игнорируемые серверы по-прежнему опрашиваются и попадают в метрики — их просто никогда не запускают, поэтому замороженный будет висеть с `selectel_server_up 0` всё время, пока он в списке. Отдельная метрика для ignore-списка позволяет в алертах отличить «заморожен, и никто его не поднимет» от «заморожен намеренно»:

```promql
# Замороженные серверы, которые должны были подняться автоматически
selectel_server_up == 0 and selectel_server_ignored == 0

# Серверы, которые сейчас в ignore-списке
selectel_server_ignored == 1
```

Наборы лейблов совпадают, поэтому `on(...)` в этих запросах не нужен.
