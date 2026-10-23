# GP Plane Core

GP Plane Core — самостоятельный управляющий контур Linux для поиска стратегий
zapret2. Core владеет auth, данными, заданиями, vault и запуском через root-helper.
Web устанавливается отдельно и обращается к Core по HTTP.

Тестовый source snapshot **0.4.5a1** разделения после v0.4.4. Это не опубликованный
alpha-релиз и не stable; устанавливать только в изолированную тестовую среду.
Новый стабильный выпуск ещё не опубликован; stable update selector не меняется.
История, теги и выпуски прежнего GP сохранены в том же репозитории
[balbomush/GP-plane](https://github.com/balbomush/GP-plane).
Опубликованный v0.4.4 — прежний монолит; его артефакты не заменяются этим срезом.

## Самостоятельная сборка

Python >=3.11, Bottle 0.13.4, Cheroot 11.1.2. Node и пакет Web не нужны.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'setuptools==84.0.0' build
.venv/bin/python -m build --no-isolation
python3 -m venv /tmp/gp-core-check
/tmp/gp-core-check/bin/python -m pip install --no-index --find-links /path/to/wheelhouse dist/*.whl
cd /tmp
/tmp/gp-core-check/bin/gp-control-plane --state-dir /tmp/gp-core-state core --host 127.0.0.1 --port 8081
```

Для qualification используйте отдельные каталоги и точные wheel/checksum; `/tmp`
в примере не является штатным хранилищем. Продуктовые зависимости — в
`requirements/a5-transport.lock`. Собственные Core tests и интеграционные проверки
разделены по ответственности; [HTTP/runtime](docs/headless-runtime-core-api.md).

Собственный набор: `python -m unittest discover -s tests`. Linux shell/socket,
symlink и аппаратные сценарии проверяются на Linux, отдельно от Windows.
[Карта владельцев](tests/OWNERSHIP.md) описывает перенос прежних смешанных тестов.
Общий artifact runner запускается Core-only интерпретатором:

```bash
/path/to/core-venv/bin/python tests/qualify_pair.py \
  --web-python /path/to/web-venv/bin/python --web-tests /path/to/web-source/tests \
  --core-wheel /path/to/core.whl --web-wheel /path/to/web.whl \
  --output /path/to/evidence/pair.json
```

Runner проверяет отдельные установленные пакеты и их точное совпадение с wheel;
отсутствующий артефакт не считается успешным skip. Browser/toolchain и real engine
имеют отдельные gates. Тестовые данные и результаты храните вне checkout.
Для browser case из standalone clone/sdist задайте `GP_BROWSER_RUNTIME` —
абсолютный путь к подготовленному BGT-001 runtime — и `GP_BROWSER_ARTIFACTS`.
Runner проверяет прежние SHA Node, версию Playwright и revision Chromium;
переменная пути не отключает проверку закреплённого toolchain.

## Core-only установка и обновление

При переходе с v0.4.4 прежняя панель отключается. До activation подготавливаются
закреплённый zapret2, Python runtime и recoverable vault; ошибка подготовки
сохраняет старые службы и данные. Во время activation сначала останавливаются
legacy Core/Web, затем заменяются файлы. Vault восстанавливается install user
до запуска служб; при ошибке restore сохранённый vault остаётся для recovery.
Откат на старый бинарник поверх восстановленных данных не поддерживается.

После публикации точного annotated stable/alpha tag bootstrap запускается как
install user, с `GP_BRANCH=<точный-тег>` и `GP_INSTALL_WEB=off`. Берите URL
bootstrap из описания этого выпуска; не подставляйте v0.4.4 для установки данного
кандидата. Alpha → stable требует отдельной clean-install от стабильного source.

Для локально квалифицированного clean candidate checkout предусмотрен тот же
installer с полной идентичностью commit, а не moving branch:

```bash
sudo bash scripts/install-linux.sh --source-dir "$PWD" --install-user "$USER" \
  --candidate-sha "$(git rev-parse HEAD)" --web off --initial-install on
```

`--initial-install on` применяется только на чистом install root без существующих
данных; для обновления нужен предварительно подготовленный vault и `off`.
Обычный опубликованный bootstrap сам проверяет единственный поддержанный source,
создаёт fresh vault до sudo и выбирает режим; ручной restore — аварийный путь.
Полный apt upgrade и reboot установщик не выполняет.

Core paths сохраняются: `~/gp/GP-access-control-plane`,
`~/gp/.GP-access-control-plane.data/state`,
`~/gp/.GP-access-control-plane.data/backups`, `gp-control-plane-core.service`.
Core слушает только `127.0.0.1:8081`. Новый отдельный Web находится в
`~/gp/GP-plane-web` и использует `gp-web.service` / `/etc/default/gp-web`.
Core update его не останавливает и не удаляет; несовместимость определяется до
activation. Legacy `gp-control-plane-web.service` к новому Web не относится.

## Первый вход без Web

Core API/Swagger: `http://127.0.0.1:8081/openapi.json` и `/swagger`.
Начальные учётные данные `admin` / `admin` смените через существующий API.
Bearer живёт 24 часа; смена пароля отзывает прежние токены.

```bash
BASE_URL=http://127.0.0.1:8081
curl -fsS "$BASE_URL/api/health"
curl -fsS -X POST "$BASE_URL/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'
```

Передайте полученный `access_token` в заголовке, без токена в URL:

```bash
curl -fsS -X POST "$BASE_URL/api/auth/change-password" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"current_password":"admin","new_password":"новый-надежный-пароль"}'
```

После смены пароля войдите снова. Подготовка vault также доступна без Web:

```bash
curl -fsS -X POST "$BASE_URL/api/core/clean-install-vaults/create" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

Эта операция сохраняет device-local vault, не выгружает секреты в браузер.
Installer проверяет pending vault и восстанавливает данные штатным путём.

## Служба и отдельный Web

```bash
gp-control-plane zapret2 check-install
sudo systemctl status gp-control-plane-core.service
journalctl -u gp-control-plane-core.service -f
```

Start/Stop/restart Core выполняются соответствующими командами systemctl только
для этой службы. Его root-helper/права не принадлежат Web.
[GP Plane Web](https://github.com/balbomush/GP-plane-web) устанавливается отдельной
командой по его README из квалифицированного wheel. Самостоятельное удаление Web
не удаляет Core/данные и не прерывает подбор. Автоматического deploy из браузера нет.

## Зависимость zapret2

Штатный установщик GP сам подготавливает закреплённый release `zapret2`.
`scripts/install-zapret2.sh` — внутренний этап подготовки из полного checkout
точного тега GP; отдельного запуска через `curl | bash` ему не требуется.
GP использует runtime из `/opt/gp-zapret2/v1.0.5.2-<архитектура>` через свои
wrapper в `/usr/local/libexec/gp-control-plane`. Существующая самостоятельная
установка `/opt/zapret2` сохраняется. Для самостоятельной установки zapret2
используйте [инструкции upstream](https://github.com/bol-van/zapret2).

## Управление подбором

Действующие операции описаны в Core OpenAPI. Standard/Multi используют
`POST /api/core/strategy-discovery/start-run`; Stop —
`POST /api/core/strategy-discovery/stop-current-run`. Журнал доступен через
`GET /api/core/runs/latest-log`; все запросы требуют Bearer.
Пример Standard с одной TLS-проверкой:

```bash
curl -fsS -X POST "$BASE_URL/api/core/strategy-discovery/start-run" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"standard","domains":["example.com"],"protocols":["tcp"],"settings":{"enable_http":false,"enable_tls12":true,"enable_tls13":false,"include_quic":false}}'
```

Полученный `run_id` связывает progress/log/history. Stop сохраняет найденные
стратегии; запуск после перезагрузки не возобновляется автоматически. Для
операторских экранов установите отдельный Web; UI не нужен для API или vault.
Подбор проверяет стратегии на этом хосте, не применяет их к трафику роутера.

## Обновление и recovery

Пользовательский маршрут — bootstrap точного опубликованного annotated tag,
указанного в release notes. Moving branch, dev и локальный candidate SHA
не являются обычной пользовательской установкой. Подготовка и штатный restore
vault описаны выше; старые теги/артефакты сохраняют прежнее поведение.

Поддерживаются ровно два прежних source пути:
`$HOME/gp/GP-access-control-plane/build/state` или
`$HOME/gp/.GP-access-control-plane.data/state`. При наличии обоих bootstrap
останавливается до `sudo`; внешние state paths не входят в эту миграцию.
Найденный pending vault при живом source также останавливает повтор до sudo:
повторное восстановление устаревшего снимка вместо текущих данных запрещено.

## Данные И Бекапы

По умолчанию локальные данные лежат здесь:

```text
~/gp/.GP-access-control-plane.data/state/
```

Файловые бекапы лежат здесь:

```text
~/gp/.GP-access-control-plane.data/backups/
```

При strict clean-install прежние данные, находившиеся внутри каталога проекта, экспортируются в device-local vault и штатно восстанавливаются автоматически до первого запуска сервисов. Ручное подтверждение через HTTP/API относится только к аварийному восстановлению сохранённого pending vault.

Откат кода не откатывает пользовательские данные и не отменяет этот перенос. Для возврата данных используйте созданный ранее бекап. Данные остаются на хосте и никуда не публикуются.

## Лицензия

Собственный код GP распространяется по [MIT](LICENSE), Copyright (c) 2026 balbomush. Сторонние компоненты сохраняют свои условия: [уведомления](THIRD_PARTY_NOTICES.md).

Установщик использует отдельный каталог /opt/gp-zapret2/v1.0.5.2-<архитектура>; существующий /opt/zapret2, его настройки и чужие файлы сохраняются. Повтор использует только совпадающие проверенные файлы GP runtime; конфликт завершает подготовку с ERROR без остановки прежней GP. При сбое подготовки сети, checksum, архива, архитектуры или Python runtime старые службы и данные сохраняются; pending vault при живом source требует устранить причину перед следующим штатным запуском.
