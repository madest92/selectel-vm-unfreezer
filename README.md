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

Every setting is a CLI flag or an environment variable (CLI wins). `SELECTEL_*` env vars keep that prefix to avoid clashing with vars like `USER`.

| CLI flag                       | Environment variable       | Default      | Description                                                                      |
|--------------------------------|----------------------------|--------------|----------------------------------------------------------------------------------|
| `--host`                       | `HOST`                     | `0.0.0.0`    | Address the metrics HTTP server binds to                                         |
| `--port`                       | `PORT`                     | `8000`       | Port the metrics HTTP server listens on                                          |
| `--account`                    | `SELECTEL_ACCOUNT`         | *(required)* | Selectel account (domain) name/ID                                                |
| `--project`                    | `SELECTEL_PROJECT`         | *(required)* | Selectel project name                                                            |
| `--user`                       | `SELECTEL_USER`            | *(required)* | Selectel service user name                                                       |
| `--password`                   | `SELECTEL_PASS`            | *(required)* | Selectel service user password (prefer the env var, see note below)              |
| `--region` (repeatable)        | `SELECTEL_REGIONS`         | *(auto)*     | Region(s) to monitor, e.g. `ru-7`, `ru-3`; env var is comma-separated            |
| `--ignore-server` (repeatable) | `SELECTEL_IGNORED_SERVERS` | *(none)*     | Server name(s) or ID(s) to never start automatically, optionally `REGION/SERVER` |
| `--metric-update-interval`     | `METRIC_UPDATE_INTERVAL`   | `10`         | Seconds between status refresh cycles                                            |
| `--log-level`                  | `LOG_LEVEL`                | `info`       | Logging verbosity: `debug`, `info`, `warning`, `error`                           |

> Prefer `SELECTEL_PASS` over `--password` — CLI args are visible to other users via `ps aux`.
>
> Leave `--region` unset to have the exporter discover your regions itself.

## Usage

```bash
python main.py \
  --account my-account --project my-project \
  --user server-monitor --password "$SELECTEL_PASS" \
  --ignore-server staging-db
```

Or via Docker, using the environment variables instead (a prebuilt image is published to `ghcr.io/madest92/selectel-vm-unfreezer`; use it instead of building locally):

```bash
docker run -d -p 8000:8000 \
  -e SELECTEL_ACCOUNT=my-account -e SELECTEL_PROJECT=my-project \
  -e SELECTEL_USER=server-monitor -e SELECTEL_PASS=secret \
  -e SELECTEL_IGNORED_SERVERS=staging-db,test-vm \
  ghcr.io/madest92/selectel-vm-unfreezer:latest
```

Full flag list: `python main.py --help`.

## Metrics

Served at `http://localhost:8000/metrics`:

| Metric                    | Labels                               | Values                          | Description                                                        |
|---------------------------|--------------------------------------|---------------------------------|--------------------------------------------------------------------|
| `selectel_server_up`      | `server_name`, `server_id`, `region` | `1` = running, `0` = frozen     | Status as reported by Selectel                                     |
| `selectel_server_ignored` | `server_name`, `server_id`, `region` | `1` = ignored, `0` = managed    | Whether the server is excluded from automatic start                |
| `selectel_region_up`      | `region`                             | `1` = polled, `0` = unreachable | Whether the last poll succeeded; `0` = servers unknown, not absent |

```promql
# Frozen servers that should have been started automatically
selectel_server_up == 0 and selectel_server_ignored == 0

# Regions we are currently blind to
selectel_region_up == 0
```

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

Любой параметр — это флаг CLI или переменная окружения (флаг важнее). Префикс `SELECTEL_*` нужен, чтобы не пересекаться с переменными вроде `USER`.

| Флаг CLI                            | Переменная окружения       | По умолчанию   | Описание                                                                                |
|-------------------------------------|----------------------------|----------------|-----------------------------------------------------------------------------------------|
| `--host`                            | `HOST`                     | `0.0.0.0`      | Адрес, на котором поднимается HTTP-сервер метрик                                        |
| `--port`                            | `PORT`                     | `8000`         | Порт HTTP-сервера метрик                                                                |
| `--account`                         | `SELECTEL_ACCOUNT`         | *(обязателен)* | Аккаунт (домен) Selectel                                                                |
| `--project`                         | `SELECTEL_PROJECT`         | *(обязателен)* | Имя проекта Selectel                                                                    |
| `--user`                            | `SELECTEL_USER`            | *(обязателен)* | Имя сервисного пользователя Selectel                                                    |
| `--password`                        | `SELECTEL_PASS`            | *(обязателен)* | Пароль сервисного пользователя (лучше через переменную окружения, см. ниже)             |
| `--region` (можно повторять)        | `SELECTEL_REGIONS`         | *(авто)*       | Регион(ы) для мониторинга, например `ru-7`, `ru-3`; в env — список через запятую        |
| `--ignore-server` (можно повторять) | `SELECTEL_IGNORED_SERVERS` | *(нет)*        | Имена или ID серверов, которые не запускать автоматически, можно с префиксом `REGION/`  |
| `--metric-update-interval`          | `METRIC_UPDATE_INTERVAL`   | `10`           | Интервал между циклами опроса, в секундах                                               |
| `--log-level`                       | `LOG_LEVEL`                | `info`         | Уровень логирования: `debug`, `info`, `warning`, `error`                                |

> Используйте `SELECTEL_PASS` вместо `--password` — аргументы командной строки видны другим пользователям через `ps aux`.
>
> Если не задавать `--region`, экспортёр сам определит регионы вашего аккаунта.

### Использование

```bash
python main.py \
  --account my-account --project my-project \
  --user server-monitor --password "$SELECTEL_PASS" \
  --ignore-server staging-db
```

Либо через Docker, используя переменные окружения (готовый образ публикуется в `ghcr.io/madest92/selectel-vm-unfreezer`, собирать локально не нужно):

```bash
docker run -d -p 8000:8000 \
  -e SELECTEL_ACCOUNT=my-account -e SELECTEL_PROJECT=my-project \
  -e SELECTEL_USER=server-monitor -e SELECTEL_PASS=secret \
  -e SELECTEL_IGNORED_SERVERS=staging-db,test-vm \
  ghcr.io/madest92/selectel-vm-unfreezer:latest
```

Полный список параметров: `python main.py --help`.

### Метрики

Отдаются на `http://localhost:8000/metrics`:

| Метрика                   | Лейблы                               | Значения                              | Описание                                                              |
|---------------------------|--------------------------------------|---------------------------------------|-----------------------------------------------------------------------|
| `selectel_server_up`      | `server_name`, `server_id`, `region` | `1` — работает, `0` — заморожен       | Статус, который сообщает Selectel                                     |
| `selectel_server_ignored` | `server_name`, `server_id`, `region` | `1` — игнорируется, `0` — управляется | Исключён ли сервер из автозапуска                                     |
| `selectel_region_up`      | `region`                             | `1` — опрошен, `0` — недоступен       | Удался ли последний опрос; `0` — серверы неизвестны, а не отсутствуют |

```promql
# Замороженные серверы, которые должны были подняться автоматически
selectel_server_up == 0 and selectel_server_ignored == 0

# Регионы, которых мы сейчас не видим
selectel_region_up == 0
```
