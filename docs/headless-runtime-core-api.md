# Headless Runtime / Core API

> Историческое описание API 1 и split-runtime. Для текущего API 2, запуска и установки см. [README](../README.md) и [OpenAPI](../openapi-v2.json).

Статус: alpha-этап split-runtime. В feature-ветке добавлены API-only команда `gp-control-plane core`, proxy-режим `gp-control-plane web --core-url ...` и installer-flow, который по умолчанию поднимает Core service и Web proxy service.

Постоянный API-контракт первого уровня находится в [`../openapi.json`](../openapi.json). При изменении API этот файл должен обновляться вместе с кодом.

Для человека контракт доступен через Swagger UI: `/swagger`. Raw JSON отдается через `/openapi.json`. При обычной установке оба маршрута открываются через Web service на `http://<board>:8080` и показывают полный Web/Core/Service контракт. В headless-only режиме локальный Core API `http://127.0.0.1:8081/openapi.json` показывает только callable Core/Service/OpenAPI операции.

## Решение По Runtime

Выбран вариант А: штатный Web UI остается доступен пользователю по одному внешнему адресу, а Web service проксирует зарегистрированные Core/Service routes в локальный Core service. `/openapi.json` Web service отдает локально как полный контракт.

Реализуемая схема:

```text
Browser / штатный Web UI
        |
        | http://<board>:8080
        v
gp-control-plane-web.service
        |
        | proxy registered /api/core and /api/service routes -> http://127.0.0.1:8081
        | local /api/web/*
        v
gp-control-plane-core.service
        |
        | privileged actions
        v
gp-root-helper
```

Headless-сценарий устанавливает только `gp-control-plane-core.service`. Штатный Web UI при этом не удаляется из проекта: пользователь может включить его установкой отдельного Web service или подключить собственный UI к Core API с bearer-аутентификацией.

## Цели

- Сделать `core service` единственным источником истины для логики GP, job-runner, storage, состояния, результатов подбора, backup/restore, диагностики GP и API.
- Оставить штатный Web UI как обычного клиента Core API, а не как владельца продуктовой логики.
- Дать продвинутому пользователю возможность поставить сервис без штатного Web UI и подключить собственный UI/дашборд.
- Не ломать текущие установки и текущую команду `gp-control-plane web` до отдельного решения о миграции.

## Не Цели Alpha-Этапа

- Не разделять текущий процесс на два systemd-сервиса в коде.
- Не удалять штатный Web UI.
- Не менять модель bearer-аутентификации, уже входящую в контракт.
- Не включать внешний bind Core API по умолчанию: его по-прежнему задает пользователь явной настройкой.
- Не делать два разных формата состояния для headless и web.
- Legacy root-level `/api/...` endpoint'ы удалены в alpha-этапе `WEB-LEGACY-001`; фактический контракт - Swagger/OpenAPI (`/swagger`, `/openapi.json`). Compatibility layer и таблица old-to-new mapping до 1.0 не поддерживаются.

## Текущее Состояние Compatibility-Mode

`gp-control-plane web` без `--core-url` остается compatibility-mode и запускает единый HTTP-сервер. В нем находятся:

- HTML/CSS/JS штатного интерфейса.
- namespaced API `/api/core/*`, `/api/service/*`, `/api/web/*`.
- Запуск/остановка подбора.
- Чтение storage, backups, candidates, runs, settings, release metadata.
- Вызов `gp-root-helper` для привилегированных операций.

Такой режим сохраняется только как compatibility-mode процесса. Установщик в обычном режиме использует split-runtime: `gp-control-plane core` + `gp-control-plane web --core-url http://127.0.0.1:8081`.

## Ответственность Процессов

### Core Service

`gp-control-plane-core.service` владеет продуктовой логикой и состоянием:

- запуск, остановка и прогресс подбора;
- единая нормализация входа/выхода для всех режимов подбора;
- storage, доменные списки, связи домен-стратегия, результаты, кандидаты, история запусков;
- backup/export/import/restore внутренних данных GP;
- run settings, которые реально влияют на подбор;
- нормализованные product events;
- диагностика готовности GP к работе;
- служебные операции GP, которые не являются UI-state.

Core service не должен отдавать готовые UI-карточки, локализованные подсказки, цветовые severity или системный мониторинг платы вроде CPU/RAM/load.

### Web Service

`gp-control-plane-web.service` владеет штатным Web UI:

- отдает статические ресурсы интерфейса;
- проксирует только зарегистрированные Core/Service routes в локальный Core API;
- отдает `/openapi.json` локально как полный контракт, включая `/api/web/*`;
- обслуживает `/api/web/...` локально для UI-оптимизированных срезов данных, если они нужны штатному UI;
- возвращает локальный 404 для legacy/unknown API без чтения body и без forwarding;
- не владеет продуктовой логикой, storage и job-runner.

`/api/web/...` допускается только там, где штатному UI нужен отдельный экранный срез: пагинация, ограниченная выборка, сортировка или формат списка под конкретную вкладку. Если данные являются полным продуктовым фактом, они должны идти через `/api/core/...` или `/api/service/...`.

### Root Helper

`gp-root-helper` остается отдельной привилегированной границей:

- запуск `blockcheck2`/`nfqws2` с нужными правами;
- остановка процессов и cleanup сетевых артефактов;
- операции, которые нельзя безопасно выполнять из непривилегированного Core process.

Core API вызывает root-helper через явные команды. Web service не должен вызывать root-helper напрямую.

## Network И Bind Model

Целевой default для split-runtime:

- `gp-control-plane-web.service`: `0.0.0.0:8080` или другой явно заданный Web bind/port. Это внешний адрес пользователя.
- `gp-control-plane-core.service`: `127.0.0.1:8081` по умолчанию при установленном Web service.
- Web service проксирует только зарегистрированные `/api/core/*` и `/api/service/*` routes в Core service; `/api/web/*` и `/openapi.json` обслуживаются Web локально.
- Legacy/unknown API возвращают локальный 404 без body parsing/forwarding.
- Headless/non-interactive install может не ставить Web service. Внешний bind для Core API включается только явной настройкой пользователя; перед открытием API в LAN следует сменить исходный пароль.

Причина такого выбора: пользователь продолжает работать по одному адресу платы, CORS не появляется, Web UI не требует знания второго порта, а headless-сценарий остается возможным.

## API Namespace

| Namespace | Смысл |
| --- | --- |
| `/api/core/...` | Основной функционал продукта и внутренние данные GP. |
| `/api/service/...` | Состояние установленного GP, релизы, service/unit, внешние источники и репозитории. |
| `/api/web/...` | UI-оптимизированные срезы для штатного Web UI. |

## Аутентификация И Swagger

Bearer-аутентификация входит в постоянный OpenAPI-контракт и работает одинаково в compatibility, split-runtime и headless Core mode. При новом state-dir исходные учетные данные: `admin` / `admin`. Смените этот пароль до включения внешнего bind Core API.

- `GET /api/health` и `POST /api/auth/login` доступны без токена.
- `POST /api/auth/login` принимает `username` и `password` и возвращает `access_token`, `token_type: "Bearer"` и `expires_in: 86400`: токен действует 24 часа.
- Все защищенные зарегистрированные API-операции, включая Core и Service, требуют заголовок `Authorization: Bearer <access_token>`.
- `POST /api/auth/change-password` также требует bearer-токен. Он принимает текущий пароль и `new_password` длиной не менее 8 символов, возвращает новый токен и делает ранее выданные токены недействительными.

Чтобы выполнить защищенный запрос через Swagger:

1. Откройте `/swagger` и выполните `POST /api/auth/login` через **Try it out** с текущими учетными данными.
2. Скопируйте из ответа только значение `access_token`.
3. Нажмите **Authorize**, выберите `bearerAuth` и вставьте это значение без префикса `Bearer`.
4. Выполняйте защищенные операции: Swagger добавит заголовок `Authorization: Bearer <access_token>` сам. Авторизация сохраняется в Swagger UI для текущего браузера.

В headless-only режиме используйте Swagger по адресу `http://127.0.0.1:8081/swagger` либо обращайтесь к API напрямую с тем же заголовком. Core-only OpenAPI включает callable Core/Service операции, а также маршруты аутентификации и `/openapi.json`.

Разделение `/api/core/public/...` и `/api/core/private/...` не используется: доступ определяется OpenAPI security scheme и требованием токена для конкретной операции, а не названием URL.

Endpoint names должны быть человекочитаемыми. Для action endpoint'ов сохраняется прагматичный POST-action стиль: `POST /save-domain-list`, `POST /delete-user-domain-list`, `POST /check-updates`.

## Core API Контуры

### Strategy Discovery

Основной внешний endpoint запуска:

- `POST /api/core/strategy-discovery/start-run`

Он принимает `mode` и работает как маршрутизатор режима подбора. Отдельные низкоуровневые endpoint'ы для конкретного режима допускаются только когда режим имеет самостоятельный продуктовый смысл для внешнего клиента.

В `start-run` уходят итоговые домены и параметры запуска:

- конкретный массив доменов;
- протоколы;
- timeout values;
- `curl_parallelism`;
- явный объект `settings` для общих run settings.

`start-run` принимает только top-level поля `mode`, `domains`, `protocols`, `curl_parallelism`, `timeout_seconds`, `settings`. Внутри `settings` принимаются только реальные runtime-параметры подбора: curl timeouts, protocol flags, IPv6/debug, scan level и repeats. Скрытые mode-specific поля вроде `mode_settings` не поддерживаются и не должны прокидываться в job payload.

В запуск не уходит `preset id`, filter id или живая ссылка на список. Список доменов является только шаблоном заполнения пользовательского поля. История запуска хранит итоговые домены и настройки, но не хранит `source_preset`.

Сопутствующие endpoint'ы:

- `POST /api/core/strategy-discovery/stop-current-run`;
- `GET /api/core/strategy-discovery/current-run-progress`;
- `GET /api/core/strategy-discovery/current-run-latest-log`;
- `GET /api/core/strategy-discovery/preflight`.

### Presets И Domain Lists

Пресеты доменов являются сохраненными доменными списками:

- обязательный список;
- желательный список;
- пользовательские списки.

Будущая замена текущего `GET /api/strategy-finder/domains`:

- `GET /api/core/presets/domain-lists`

Метод возвращает только реально существующие сохраненные списки: первым обязательный, вторым желательный, далее пользовательские списки в порядке хранения. Старые hardcoded discovery-наборы не переносятся.

Операции:

- `POST /api/core/presets/save-domain-list` - сохранить один системный или пользовательский список;
- `POST /api/core/presets/delete-user-domain-list` - удалить один или несколько пользовательских списков через непустой массив `list_ids`.

Bulk-save всех пользовательских списков и включение/выключение отдельного домена внутри списка в новую схему не переносятся. Продуктовая модель простая: домен либо входит в список, либо не входит.

### v2fly Domain Helper

`v2fly/domain-list-community` трактуется как помощник для наполнения пользовательских списков, а не отдельная сущность пресетов.

Core read-only методы:

- `GET /api/core/presets/v2fly/categories`;
- `GET /api/core/presets/v2fly/category-domains?category=...`.

Preview/import endpoint'ы не переносятся. UI или внешний клиент читает домены категории, редактирует итоговый набор и сохраняет обычным `POST /api/core/presets/save-domain-list`.

Если локальное хранилище v2fly не готово, `categories` возвращает `200` со `storage.state=missing`, а `category-domains` для отсутствующей категории возвращает простой error payload `{"error":"..."}` со статусом `400`. Отдельный Core readiness endpoint для v2fly не нужен.

### Backups

Backup/export/import/restore внутренних данных GP относится к Core:

- `POST /api/core/backups/create`;
- `GET /api/core/backups/list`;
- `POST /api/core/backups/restore`;
- `POST /api/core/backups/delete`;
- `GET /api/core/backups/download-archive`;
- `POST /api/core/backups/upload`.

Отдельный restore-preview endpoint не переносится. Штатный UI перед восстановлением показывает простое окно подтверждения выбранного snapshot.

### Run Settings

Настройки, влияющие на реальный подбор, относятся к Core:

- `GET /api/core/run-settings`;
- `POST /api/core/run-settings/save`.

Минимальный состав:

- `curl_parallelism_default`;
- `curl_parallelism_max`;
- `curl_max_time`;
- `curl_max_time_quic`;
- `curl_max_time_doh`;
- `enable_ipv6`;
- `debug_stdout`.

UI-state вроде выбранной вкладки, раскрытых панелей, фильтров экрана и page size не хранится на сервере.

### Runs, Candidates, Events

Полные продуктовые данные:

- история запусков - `/api/core/runs/...`;
- run logs и latest-log - `/api/core/runs/...`;
- кандидаты стратегий в JSON - `/api/core/strategy-candidates` только с фильтрами `domain/domains/strategy_id/protocol/source_mode/family/query`;
- полная или широкая выгрузка кандидатов - `/api/core/strategy-candidates/export` как `application/x-ndjson`, одна стратегия-кандидат на строку;
- нормализованные product events - `/api/core/events`.

Пагинация и экранные срезы для штатного UI относятся к `/api/web/...`. Core JSON endpoint не должен использовать пагинацию как основной способ защиты от больших ответов: если запрос широкий, клиент должен уточнить фильтр или перейти на потоковый export.

## Service API Контуры

### Status

- `GET /api/service/status` отвечает на вопрос: жив ли установленный GP service и в каком состоянии его данные/установка.
- `GET /api/core/status` отвечает на вопрос: что сейчас делает продуктовый контур GP.
- Широкий агрегат `GET /api/service/diagnostics` не входит в новый Core API surface. Его прежний смысл разнесен по конкретным методам: service state - `/api/service/status`, v2fly state - `/api/service/v2fly/local-storage-status`, готовность подбора и zapret2/root-helper/curl/nft checks - `/api/core/strategy-discovery/preflight`.

Системные метрики платы вроде CPU/RAM/load не входят в GP API.

### Releases

`GET /api/service/releases/available` только сообщает о доступных stable и
prerelease-релизах. Он не запускает установку, не выбирает канал и не строит
план установки.

Web UI и API не устанавливают релиз. Пользовательская установка выполняется
только повторным запуском `bootstrap-linux.sh` с выбранным exact annotated
release tag в `GP_BRANCH`; актуальная команда приведена в README.

### v2fly Local Storage

Состояние и обновление локального v2fly storage относится к service:

- `GET /api/service/v2fly/local-storage-status`;
- `POST /api/service/v2fly/check-updates`;
- `POST /api/service/v2fly/update-local-storage`.

## Installer Flow

Interactive install:

1. Default: установить штатный Web UI.
2. При default-установке создаются два сервиса: `gp-control-plane-core.service` и `gp-control-plane-web.service`.
3. Web service запускается с `--core-url http://127.0.0.1:8081` и проксирует зарегистрированные Core/Service routes в Core service; `/api/web/*` и полный `/openapi.json` остаются локальным Web API.
4. Если пользователь выбирает headless, создается только Core service.

Headless/non-interactive install:

1. `GP_INSTALL_WEB=off` отключает Web UI и устанавливает API-only Core service.
2. Установщик не зависает на вопросе в non-interactive режиме.
3. Core API bind по умолчанию остается локальным: `127.0.0.1:8081`.
4. Внешний bind для Core API задается явно через `GP_CORE_HOST` и `GP_CORE_PORT`.

Текущий install command должен продолжать работать. Это означает, что в переходный период установка по умолчанию сохраняет пользовательский опыт `http://<board>:8080/`.

## Systemd Flow

Целевая модель:

- `gp-control-plane-core.service`
  - запускает Core API;
  - владеет job-runner;
  - работает с state-dir;
  - вызывает root-helper;
  - пишет core/runtime логи.

- `gp-control-plane-web.service`
  - зависит от Core service;
  - отдает штатный UI;
  - проксирует зарегистрированные Core/Service routes в Core;
  - отдает полный `/openapi.json` локально;
  - обслуживает `/api/web/*` локально и возвращает локальный 404 для legacy/unknown API;
  - может быть не установлен в headless-сценарии.

Compatibility-mode:

- команда `gp-control-plane web` без `--core-url` остается рабочей как старый единый web-режим;
- старый единый web-режим не удаляется без отдельного решения;
- установленный default-runtime использует split: Core service + Web proxy service.

## Поддерживаемая Установка И Перенос Данных

### Постоянный Каталог Данных (правило, введённое в v0.4.0)

Для новой рабочей установки каталог данных является соседним с каталогом проекта. Например, при `~/gp/GP-access-control-plane` состояние находится в `~/gp/.GP-access-control-plane.data/state`, а файловые бекапы — в `~/gp/.GP-access-control-plane.data/backups`.

Маршрут релиза v0.4.0 не являлся update или возвратом к старой установке.
Это историческое описание границы миграции; текущую версию поставки и команду
установки задаёт README. Поддерживается только односторонняя последовательность:

`vault → fresh install → verified restore`.

Legacy-приложение создаёт vault на той же плате. Затем fresh install удаляет
только фиксированную GP-поверхность и ставит выбранный exact annotated stable
или alpha release tag. До остановки прежней GP installer завершает подготовку
`apt`, закреплённого release zapret2 (digest, распаковка, архитектура, binaries/Lua)
и Python wheels GP с проверкой импорта. Существующий `/opt/zapret2` сохраняется;
GP runtime живёт в отдельном root-owned каталоге `/opt/gp-zapret2/`.
Затем installer удаляет прежнюю GP-поверхность, создаёт wrapper и fresh files,
создаёт venv и устанавливает уже проверенные wheels без сети. Только
после этого он автоматически запускает user-side restore; restore выполняется
до `domain-sources prepare-v2fly` и до любого `systemctl enable --now` запуска
сервиса. Продукт проверяет импорт и SQLite. Источник удаляется только при
`completed=true`, `verification.verified=true`, `storage_status.ready=true` и
`cleanup.source_deleted=true`. При сбое vault/handoff остаются на этой же
плате для повторного fresh install или аварийного HTTP/API restore. Vault, handoff, raw SQLite и
ZIP не переносятся между платами. Root не читает, не копирует и не удаляет эти
user-owned private материалы.

Эта документация не задаёт privileged transport, snapshot или историческую
процедуру возврата. Они не являются текущим эксплуатационным маршрутом.

### Durable/Ephemeral State Plan

Цель: убрать долговременные пользовательские данные из `state.json`, оставив там только короткое runtime/compatibility-состояние процесса.

Целевая граница:

- SQLite durable: домены, стратегии, связи стратегия-домен, пользовательские и системные доменные списки, настройки запуска подбора, история запусков, backup metadata.
- `state.json` ephemeral: `current_job`, `current_job_name`, `current_job_status`, `last_job_status`, `last_error`, короткие compatibility-поля для старых UI/API до их явной миграции.
- Файлы логов ephemeral: stdout/stderr/progress/metrics текущего или последнего run, с ограниченным чтением через API.

Очередь реализации:

Статус текущего переходного этапа: пункты 1-3 реализованы. `app_settings` добавлен в SQLite schema v11; `GET /api/core/run-settings` и `POST /api/core/run-settings/save` используют SQLite как основной источник, а `state.json.settings` остаётся временной совместимой копией. Backup schema v6 переносит `app_settings`; старые snapshot schema v5 поддерживаются, но настройки не заменяют.

Статус модульного split: `index_html()` вынесен в `web/ui.py`, Swagger/OpenAPI helpers - в `web/docs.py`, Web proxy - в `web/proxy.py`, Core entrypoint - в `web/core_server.py`; `web.app` сохраняет compatibility-wrapper'ы для старых импортов.

Бюджет ресурсов и ручные проверки Pi2/Pi5 описаны в [`resource-budget.md`](resource-budget.md). Feature-ветки не измеряются на Pi2; фактический RSS gate выполняется после main/release-candidate.

1. Добавить в SQLite таблицу `app_settings(key, value_json, updated_at)` и функции чтения/записи настроек запуска подбора.
2. Перевести `GET /api/core/run-settings` и `POST /api/core/run-settings/save` на SQLite, но оставить fallback чтения старого `state.json.settings`.
3. При первом успешном чтении старых `state.json.settings` записать их в SQLite, не удаляя из `state.json` в этом же релизе.
4. После стабильного релиза удалить запись новых `settings` в `state.json`; HTTP compatibility для legacy `/api/settings` в alpha уже удалена.
5. `run_preferences` оставить в web/ephemeral state до отдельного решения, потому что это состояние формы UI, а не Core product data.

Если SQLite migration не проходит, сервис не должен очищать `state.json`; API возвращает понятную ошибку storage/data state. Backup/restore включает `app_settings` в schema v6; старые snapshot schema v5 не трогают настройки.

Минимальные тесты:

- старый `state.json.settings` мигрирует в SQLite и сохраняет значения `curl_parallelism_max`, `curl_max_time`, `enable_ipv6`;
- `POST /api/core/run-settings/save` пишет в SQLite и не зависит от `state.json.settings`;
- backup/restore переносит `app_settings`;
- legacy HTTP `/api/settings` удален; миграция старых `state.json.settings` остается покрытой без сохранения старого URL.

Безопасная последовательность внедрения после research:

1. Добавить OpenAPI validation в локальные проверки.
2. Добавить новые `/api/core/...`, `/api/service/...`, `/api/web/...` endpoint'ы в текущий API.
3. Перевести штатный Web UI на новые endpoint'ы.
4. Выполнено: legacy root-level `/api/...` URL удалены; proxy отклоняет legacy/unknown API как 404 и не ведет old-to-new mapping table.
5. Выполнено: `index_html`, Swagger/OpenAPI helpers, Web proxy и Core entrypoint вынесены в отдельные модули.
6. Выполнено: resource budget для Pi2 зафиксирован в `docs/resource-budget.md`, backup upload снижен до 64 MiB, streaming chunks вынесены в `resource_budget.py`.

## Compatibility Decisions

Зафиксировано:

- текущий `gp-control-plane web` остается рабочим;
- текущая команда установки остается рабочей и по умолчанию поднимает Core service + Web proxy service;
- Web UI по умолчанию остается доступен на одном внешнем адресе;
- постоянный API-контракт - `openapi.json`;
- `api_inventory.md` является временным черновиком исследования.

Вне текущего alpha-решения остается только политика внешнего раскрытия Core API в LAN: bearer-аутентификация уже действует, но внешний bind не включается по умолчанию и требует явной настройки пользователя.

## Минимальная Проверка Перед Переносом В Ordinary

Перед тем как переносить split-runtime из research в обычную разработку, нужно иметь:

- валидный `openapi.json`;
- список endpoint'ов первого implementation-этапа;
- alpha-решение по старым URL зафиксировано: legacy root-level `/api/...` удалены, compatibility layer/aliasing до 1.0 не добавляется;
- тесты для legacy/unknown API 404 без old-to-new mapping;
- installer/systemd сценарии для default Web UI и headless install;
- оценку нагрузки на слабой плате только как архитектурную проверку, без релизного использования feature-ветки на контрольной плате.
