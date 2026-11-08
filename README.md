# GP Plane Core

Самостоятельный сервис Linux для поиска стратегий zapret2. Core хранит
пользовательские данные, выполняет поиск через управляемый root-helper и
обслуживает API. Эта ветка реализует API 2 draft.33 и перевод базы v11 на v12.

Опубликованный v0.5.0 остаётся выпуском с API 1. У API 2 пока нет отдельного
релизного тега. Клиентам нужно перейти на новый контракт.

## Core-only установка и обновление

Локальный чистый checkout проверяется по полному commit:

```bash
sudo bash scripts/install-linux.sh --source-dir "$PWD" --install-user "$USER" \
  --candidate-sha "$(git rev-parse HEAD)" --web off --mode ordinary
```

Режим `ordinary` по умолчанию. При существующей управляемой установке он
готовит новый пакет и зависимости до остановки Core, сохраняет данные,
доступ и конфигурацию службы. При первом запуске новой программы Core
переводит поддерживаемую v11 на v12. При сбое запуска старый комплект
сохраняется для диагностики. Автоматическое понижение версии отсутствует.

`--mode clean` явно удаляет прежнее состояние Core. Для переноса
пользовательских данных создайте backup заранее и восстановите его после
первого запуска. Новый пароль целевой установки при этом сохраняется.

Для опубликованной поставки используйте bootstrap из её описания.
`GP_BRANCH` задаёт точный annotated tag, `GP_INSTALL_MODE` — `ordinary`
или `clean`. `GP_INSTALL_WEB=off`. Установщик управляет только Core и его
закреплённым zapret2 в `/opt/gp-zapret2`. Самостоятельный `/opt/zapret2`,
Web и его служба остаются под управлением владельца.

Штатные пути Core:

- Программа `~/gp/GP-access-control-plane`
- Данные `~/gp/.GP-access-control-plane.data/state`
- Служба `gp-control-plane-core.service`
- HTTP по умолчанию `http://127.0.0.1:8081`

## Первый вход и поиск

Полный API доступен в `/openapi.json` и `/swagger/`. В чистой установке
начальные данные `admin` / `admin`. Смена пароля отзывает прежние токены.

```bash
BASE_URL=http://127.0.0.1:8081
curl -fsS "$BASE_URL/api/health"
curl -fsS -X POST "$BASE_URL/api/v2/access/authentication" \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin"}'
```

Передавайте `bearer_token` в `Authorization: Bearer $TOKEN`. Пароль
меняется через `PUT /api/v2/access/change_password` с полями `username`,
`old_password` и `new_password`.

```bash
curl -fsS -X POST "$BASE_URL/api/v2/engines/blockcheck2/start" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domains":["example.com"]}'
```

Используйте `/api/v2/search/current`, `/api/v2/search/events`,
`POST /api/v2/search/stop` и `/api/v2/data/runs/{run_id}/log`.
Ручное продолжение доступно для совместимой позиции GP-движка через
`POST /api/v2/engines/blockcheck2-gp/runs/{run_id}/resume`.
После перезапуска поиск автоматически не продолжается.

## Backup и обслуживание

`POST /api/v2/backup` заменяет единственную сохранённую копию после
успешной подготовки. `GET /api/v2/backup/file` скачивает пользовательский
набор. `POST /api/v2/backup/restore` восстанавливает сохранённую копию,
`POST /api/v2/backup/restore-file` принимает архив
`application/octet-stream`. Копия содержит пользовательские данные,
включая результаты, файлы и историю. Учётные данные и служебные настройки
остаются у целевой установки.

При обновлении из API клиент создаёт UUID и сохраняет его до отправки
`POST /api/v2/service/updates` с полями `update_id` и `version`.
Состояние читается через `GET /api/v2/service/updates/{update_id}`,
в том числе после перезапуска Core. Повтор с тем же ID и версией читает
сохранённый отчёт. После обрыва ответа начните с чтения этого отчёта.

## Самостоятельная сборка

Требуется Python 3.11 или новее. Зависимости транспорта закреплены в
`requirements/a5-transport.lock`. Node и Web не нужны.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/a5-transport.lock
.venv/bin/python -m pip install build
.venv/bin/python -m build
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -p 'test_api_v2*.py'
```

Проверки системного установщика, helper и завершения реальных процессов
выполняются в изолированном Linux на тестовых данных.

## Лицензия

Собственный код распространяется по [MIT](LICENSE), Copyright (c) 2026
balbomush. Условия сторонних компонентов перечислены в [уведомлениях](THIRD_PARTY_NOTICES.md).