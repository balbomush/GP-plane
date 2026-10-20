# GP Access Control Plane

GP Access Control Plane - управляющий контур для Linux-хоста, который подбирает рабочие стратегии `zapret2` через `blockcheck2.sh` и дает локальную web panel для оператора.

`Control plane` здесь означает отдельный управляющий слой: GP собирает данные, запускает проверки и хранит результаты, а пользовательский трафик остается в `data plane` на роутере или другом целевом устройстве.

## Установка

### 1. Обновите систему

Перед первой установкой на чистую систему лучше отдельно обновить пакеты и перезагрузить хост:

```bash
sudo apt update
sudo apt upgrade -y
sudo reboot
```

На уже настроенной системе этот шаг остается вашим решением. Установщик GP не делает полный `apt upgrade` сам.

### 2. Запустите установщик

Обычная установка с Core service и Web UI:

```bash
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-plane/releases/download/v0.4.4/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.4 bash
```

Headless-установка без штатного Web UI:

```bash
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-plane/releases/download/v0.4.4/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.4 GP_INSTALL_WEB=off bash
```

### Alpha/prerelease (только для тестовой обратной связи)

Основные команды устанавливают стабильный выпуск. Если опубликована отдельная
alpha-сборка, её точный тег и команду установки берите из описания этого prerelease.
Номер стабильной версии не означает, что для неё существует alpha-сборка.

Переход alpha → stable и rollback не поддерживаются: alpha — самостоятельная
тестовая clean-install сборка, а не этап обновления stable-установки.

Укажите exact annotated stable tag `vX.Y.Z` или alpha tag `vX.Y.Z-alpha.N` в `GP_BRANCH`. При живом состоянии v0.4 bootstrap сначала валидирует ровно один допустимый source и создаёт fresh device-local vault; найденный pending vault при всё ещё живом source останавливает путь до `sudo`, чтобы не восстановить устаревшие данные. На хосте без source состояния bootstrap использует проверенный pending vault для повтора после уже начатой clean-install, а при отсутствии обоих выполняет non-destructive initial install без vault. Затем один штатный installer-process сначала скачивает и проверяет закреплённый release zapret2 v1.0.5.2 (SHA-256 архива, безопасная распаковка, архитектура, исполнимые binaries и Lua), собирает и проверяет Python-пакет GP. Только после успешной подготовки останавливает сервисы, удаляет только прежнюю GP-поверхность и ставит fresh версию из того же tag. При existing installation после fresh-файлов и venv installer автоматически восстанавливает vault как install user и запускает сервисы только после semantic- и SQLite-проверок; исходный vault удаляется только при успешном restore. При сбое vault до удаления ничего не меняется, а после удаления сохранённый vault остаётся для повторной clean-install или аварийного restore. Откат не поддерживается.

Установка проверяется на Debian/Ubuntu-like системах с `apt-get` и `systemd`.

После установки откройте:

```text
http://<ip-board>:8080/
```

API-контракт доступен здесь:

- Swagger UI: `http://<ip-board>:8080/swagger`;
- raw OpenAPI JSON: `http://<ip-board>:8080/openapi.json`.

В headless-only режиме эти маршруты доступны на локальном Core API: `http://127.0.0.1:8081/swagger` и `http://127.0.0.1:8081/openapi.json`. Web/monolith OpenAPI показывает полный контракт, а headless Core OpenAPI показывает только callable Core/Service/OpenAPI операции.

### Безопасность и вход

GP Control Plane v0.4.4 рассчитан на доверенную локальную сеть. Веб-интерфейс
слушает адрес платы на порту `8080`, а при первом запуске намеренно использует
учётные данные `admin` / `admin`. Это не способ безопасно публиковать сервис в
интернете или в недоверенной общей сети: не открывайте этот порт наружу и не
размещайте плату в такой сети.

Если состав или доверенность локальной сети меняются, сразу смените пароль.
Начальные учётные данные известны всем, кто читает эту документацию.

`POST /api/auth/login` выдает Bearer-токен сроком на 24 часа. Например:

```bash
curl -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}'
```

Сохраните значение `access_token` из ответа и передайте его при смене пароля:

```bash
curl -X POST "$BASE_URL/api/auth/change-password" \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"current_password":"admin","new_password":"новый-надежный-пароль"}'
```

Среди API без токена доступны только `GET /api/health` и `POST /api/auth/login`; для всех остальных API-операций требуется заголовок `Authorization: Bearer <access_token>`.

Swagger UI и raw OpenAPI можно открыть без токена. Чтобы выполнить защищенный метод через **Try it out**, сначала укажите Bearer-токен через кнопку **Authorize** в Swagger UI.

### Параметры Установки

Поддерживаются только topology-параметры, переданные в окружении bootstrap:

```bash
cat > gp-install.env <<'EOF'
GP_INSTALL_WEB=on
EOF

set -a; . ./gp-install.env; set +a
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-plane/releases/download/v0.4.4/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.4 bash
```

Проект ставится в `~/gp/GP-access-control-plane`; clean-install не принимает внешний путь состояния. Для новой рабочей установки постоянные данные хранятся рядом с каталогом проекта: состояние — в `~/gp/.GP-access-control-plane.data/state`, файловые бекапы — в `~/gp/.GP-access-control-plane.data/backups`.

Маршрут миграции v0.4.0 поддерживает один из двух стандартных путей: `$HOME/gp/GP-access-control-plane/build/state` или `$HOME/gp/.GP-access-control-plane.data/state`. При наличии обоих bootstrap останавливается до `sudo`; настроенный или внешний путь состояния не входит в scope этой миграции.

Топология выбирается только перед запуском: `GP_INSTALL_WEB=on` ставит Core и Web, `off` — только Core. Штатная clean-install для existing installation восстанавливает vault автоматически до запуска сервисов; источник удаляется только после semantic-проверки и готовности/integrity SQLite. Ручной restore через аутентифицированный HTTP/API остаётся аварийным путём для сохранённого pending vault, а не шагом обычной установки.

Что делает установщик:

- ставит нужные пакеты через `apt-get install`;
- подготавливает закреплённый `zapret2` в `/opt/gp-zapret2/v1.0.5.2-<архитектура>`, сохраняя существующий `/opt/zapret2`;
- скачивает GP и создает Python-окружение;
- устанавливает команду `gp-control-plane`;
- ставит root-helper для запуска `blockcheck2` без интерактивного sudo-пароля;
- создает и запускает systemd-сервисы.

## Проверки После Установки

Проверить root-helper и `zapret2`:

```bash
gp-control-plane zapret2 check-install
```

В выводе должны быть `root_helper_found: true` и `root_helper_ready: true`.

Проверить Web UI:

```bash
curl -I http://127.0.0.1:8080/
```

Проверить сервисы:

```bash
sudo systemctl status gp-control-plane-core.service
sudo systemctl status gp-control-plane-web.service
```

## Управление Сервисом

Старт:

```bash
sudo systemctl start gp-control-plane-core.service
sudo systemctl start gp-control-plane-web.service
```

Перезапуск:

```bash
sudo systemctl restart gp-control-plane-core.service
sudo systemctl restart gp-control-plane-web.service
```

Остановка:

```bash
sudo systemctl stop gp-control-plane-web.service
sudo systemctl stop gp-control-plane-core.service
```

Логи:

```bash
journalctl -u gp-control-plane-core.service -u gp-control-plane-web.service -f
```

Для headless-установки используйте только `gp-control-plane-core.service`.

## Зависимость zapret2

Штатный установщик GP сам подготавливает закреплённый release `zapret2`.
`scripts/install-zapret2.sh` — внутренний этап подготовки из полного checkout
точного тега GP; отдельного запуска через `curl | bash` ему не требуется.
GP использует runtime из `/opt/gp-zapret2/v1.0.5.2-<архитектура>` через свои
wrapper в `/usr/local/libexec/gp-control-plane`. Существующая самостоятельная
установка `/opt/zapret2` сохраняется. Для самостоятельной установки zapret2
используйте [инструкции upstream](https://github.com/bol-van/zapret2).

## Как Пользоваться

1. Откройте web panel: `http://<ip-board>:8080/`.
2. Во вкладке `Подбор` выберите домены.
3. Запустите обычный или экспериментальный поиск.
4. Во вкладке `Терминал` смотрите ход работы.
5. Во вкладке `Кандидаты` смотрите найденные стратегии.
6. В `Настройки` -> `Бекапы и восстановление` скачайте архив, если нужен откат.
7. Скопируйте подходящую стратегию вручную и проверьте ее там, где планируете использовать.

Подбор может длиться несколько часов. Кнопка остановки сохраняет найденные к этому моменту стратегии.

## Что Умеет Текущая Версия

- запускать локальную web panel;
- запускать подбор стратегий через штатный `blockcheck2.sh`;
- проверять одну стратегию сразу на нескольких доменах;
- ограничивать количество параллельных `curl`;
- включать и выключать проверки HTTP, TLS 1.2, TLS 1.3, HTTP3/QUIC;
- использовать встроенные пресеты доменов;
- показывать прогресс, live-лог и историю запусков;
- сохранять найденные стратегии в локальную SQLite-БД;
- показывать стратегии по доменам и общие стратегии для выбранных доменов;
- останавливать долгий подбор без потери уже найденных успешных стратегий;
- создавать и восстанавливать локальные бекапы через UI.

## Обновление

Повторно запустите bootstrap:

```bash
GP_BOOTSTRAP_URL='https://github.com/balbomush/GP-plane/releases/download/v0.4.4/bootstrap-linux.sh'
curl -LfsS "$GP_BOOTSTRAP_URL" | GP_BRANCH=v0.4.4 bash
```

Это односторонний clean-install маршрут только из exact annotated tag. Ветки, `dev`, cache/candidate routes и rollback не являются пользовательскими способами установки.

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
