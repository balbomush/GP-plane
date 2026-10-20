# S2A: Core без встроенного Web

Эта локальная feature-редакция продолжает стабильную v0.4.4 и S0/S1.
Она не опубликована, не является завершением разделения и не меняет существующие
теги/артефакты v0.4.4. Текущий стабильный выпуск содержит прежний Web.

Core distribution/namespace/CLI и пути данных сохраняются:
`gp-access-control-plane`, `gp_control_plane`, `gp-control-plane core`;
`$HOME/gp/GP-access-control-plane`, `$HOME/gp/.GP-access-control-plane.data/state`.
Переименование GitHub repository не переименовывает данные, helper или службы.

## Владение

| Модуль/объект | Владелец |
|---|---|
| `gp_control_plane.http` | Core Bottle/Cheroot listener, dispatch, auth boundary, limits, SSE, packaged OpenAPI |
| `gp_control_plane.web` | Только исторические Core import aliases на те же объекты http; Web-сервера/ресурсов здесь нет |
| `application`, `engines`, `repositories`, storage/auth/jobs/runtime/vault | Core; алгоритмы, схема и транзакции не меняются ради переноса |
| `gp_control_plane_web` / `gp-web` / `gp-web.service` | Отдельный GP Plane Web, общение с Core только HTTP |
| `packages/gp-ui` в Web | Представление результатов, чистое форматирование, callbacks; без store/auth/router/transport |
| Horizon | Собственный controller/auth/allowlist/navigation/loader/consumer/lock; подключение отдельно |

Core wheel/sdist поставляют OpenAPI в `gp_control_plane/http/openapi.json`;
`/openapi.json` остаётся фильтрованным Core-контрактом. Compatibility modules
обеспечивают идентичность объектов для прежних Core import/patch seams.
Новый Web не является Python dependency Core.

`gp-control-plane web` завершает работу с понятным сообщением и ненулевым кодом:
нужно установить отдельный Web и использовать `gp-web`. Монолитный ui_enabled
не поддерживается. `core` работает без Web и Node, в том числе auth и vault API.

## Установка и переход

Обычный clean-install/vault контракт сохранён. В этой feature-редакции bootstrap
по умолчанию Core-only; явный `GP_INSTALL_WEB=on` отклоняется до разрушительной фазы.
Прежний legacy Web (`gp-control-plane-web.service`) останавливается/отключается
вместе со старым Core до замены файлов. Ошибка остановки не разрешает удаление.
Новый `gp-web.service`, его root и config установщик Core не затрагивает.

Подготовка dependencies/package/vault и проверка владельца занятого Core-порта
предшествуют stop. Restore использует штатный vault tool и удаляет исходный vault
только после проверки. При ошибке restore службы не стартуют, vault сохраняется;
старый бинарник на изменённые данные не запускается. Helper/PATH/root protocol
сохраняются. Настоящая системная активация/перезагрузка требует отдельной приёмки.

## Границы текущего среза

Web S2A переносит только первый вертикальный результат: login/session/password,
получение существующих результатов и копирование. Остальные экраны — S3.
Один реальный Web consumer и тесты библиотеки не доказывают интеграцию Horizon.
До неё нужны точные SHA/lock/adapter/полномочия стороны Horizon и отдельный S2B.
Это не разрешение push/merge/release/deploy и не итоговый release plan.
