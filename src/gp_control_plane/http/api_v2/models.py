"""Pydantic models for the API 2 wire representation."""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, RootModel

class ErrorDetail(BaseModel):
    'Для invalid_parameters обязательны field/code/message, для busy — operation. Не включать значения паролей и токенов.'
    model_config = ConfigDict(json_schema_extra={"minProperties": 1})
    field: Annotated[str, Field(min_length=1, description='body:<JSON Pointer>, например body:/domains/1 — второй домен; query:limit, path:run_id, header:Content-Type. body: — тело целиком. Индексы с нуля.')] = Field(default_factory=lambda: cast(Any, None))
    code: Annotated[str, Field(min_length=1, description='Стабильная причина уточнения, например required, invalid_type, unknown_parameter, checkpoint_unavailable.')] = Field(default_factory=lambda: cast(Any, None))
    message: Annotated[str, Field(min_length=1, description='Понятное пояснение без секретных значений; не код для клиентской логики.')] = Field(default_factory=lambda: cast(Any, None))
    operation: Annotated[Literal['search', 'backup_create', 'backup_restore', 'archive_download', 'program_download', 'core_update', 'source_update'], Field(description='Уже выполняемая ресурсоёмкая операция; обязательна при busy.')] = Field(default_factory=lambda: cast(Any, None))


class Error(BaseModel):
    'Общий конверт. invalid_parameters: непустые details с field/code/message у каждой ошибки. busy: непустые details с operation. Остальные причины могут обходиться без details.'
    model_config = ConfigDict(json_schema_extra={'allOf': [{'if': {'properties': {'code': {'const': 'invalid_parameters'}}, 'required': ['code']}, 'then': {'required': ['details'], 'properties': {'details': {'items': {'required': ['field', 'code', 'message']}}}}}, {'if': {'properties': {'code': {'const': 'busy'}}, 'required': ['code']}, 'then': {'required': ['details'], 'properties': {'details': {'items': {'required': ['operation']}}}}}]})
    code: str
    message: str
    details: Annotated[list[ErrorDetail], Field(min_length=1)] = Field(default_factory=lambda: cast(Any, None))


class ErrorResponse(BaseModel):
    error: Error


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    username: Annotated[str, Field(min_length=1, description='Логин. Правильность проверяется вместе с паролем; сейчас существует только пользователь admin. Поле сохраняется для будущего расширения состава пользователей.', json_schema_extra={'example': 'admin'})]
    password: Annotated[str, Field(min_length=1, json_schema_extra={'format': 'password'})]


class Token(BaseModel):
    model_config = ConfigDict(extra='allow')
    bearer_token: Annotated[str, Field(min_length=1, description='Сам Bearer-токен без префикса. Передавать в Authorization: Bearer <bearer_token>.')]
    expires_at: Annotated[str, Field(pattern='Z$', description='Момент окончания действия в UTC, RFC 3339 с Z на конце.', json_schema_extra={'format': 'date-time', 'example': '2026-11-14T12:00:00Z'})]


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    username: Annotated[str, Field(min_length=1, description='Логин. Правильность проверяется вместе с паролем; сейчас существует только пользователь admin. Поле сохраняется для будущего расширения состава пользователей.', json_schema_extra={'example': 'admin'})]
    old_password: Annotated[str, Field(min_length=1, description='Текущий пароль: проверяется перед изменением.', json_schema_extra={'format': 'password'})]
    new_password: Annotated[str, Field(min_length=1, json_schema_extra={'format': 'password'})]


class Engine(BaseModel):
    id: Annotated[int, Field(ge=1)]
    code: str
    name: str


class Progress(BaseModel):
    'Только сведения о ходе работы: число проверенных стратегий, известный общий объём и текущие аргументы. Состояние поиска задаётся одним полем run.status; stage и параллельного набора статусов нет.'
    checked: Annotated[int, Field(ge=0)] | None
    total: Annotated[int, Field(ge=0)] | None
    current_args: str | None


class RunStatus(BaseModel):
    id: Annotated[str, Field(min_length=1)]
    engine: Engine | None
    milestone_id: Annotated[int, Field(ge=0)]
    status: Literal['starting', 'running', 'stopping', 'stopped', 'completed', 'failed']
    error: Error | None
    started_at: Annotated[str, Field(json_schema_extra={'format': 'date-time'})] | None
    progress: Progress


class RunDetail(BaseModel):
    'Сохранённая запись запуска: параметры, состояние и итог. Живой прогресс читается общим методом поиска. resume_available — вычисляемая возможность продолжения сохранённого задания, не гарантия немедленного допуска к выполнению.'
    id: Annotated[str, Field(min_length=1)]
    engine: Engine | None
    milestone_id: Annotated[int, Field(ge=0)]
    status: Literal['starting', 'running', 'stopping', 'stopped', 'completed', 'failed']
    error: Error | None
    started_at: Annotated[str, Field(json_schema_extra={'format': 'date-time'})] | None
    finished_at: Annotated[str, Field(json_schema_extra={'format': 'date-time'})] | None
    domains: list[str] | None
    domain_list_id: Annotated[int, Field(ge=1)] | None
    parameters: dict[str, Any] | None
    resume_available: Annotated[bool, Field(description='Можно ли продолжить это сохранённое задание: движок поддерживает resume, состояние stopped/failed, веха текущая, сохранённое задание корректно и позиция пригодна либо достоверно известна начальная позиция до первого сохранённого продвижения. Для прочих случаев и неизвестных прежних данных — false. Вычисляется при чтении; не хранится отдельной колонкой. Занятость Core и текущая готовность среды в этот признак не входят; resume заново проверяет все условия перед принятием.', json_schema_extra={'example': True})]


class RunPage(BaseModel):
    items: list[RunDetail]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]


class LogEntry(BaseModel):
    position: str
    time: Annotated[str, Field(json_schema_extra={'format': 'date-time'})] | None
    level: str
    message: str


class LogPage(BaseModel):
    entries: list[LogEntry]
    next_after: str | None
    truncated: bool


class Milestone(BaseModel):
    id: Annotated[int, Field(ge=0)]


class MilestonePage(BaseModel):
    items: list[Milestone]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]
    current_id: Annotated[int, Field(ge=1)]


class Result(BaseModel):
    'Результат по домену в вехе родительского объекта стратегии. Номер вехи читается из milestone_id родителя и не повторяется в каждой строке. ID результата сохраняется.'
    id: Annotated[int, Field(ge=1)]
    domain: str
    engines: list[Engine]


class BundleResult(BaseModel):
    'Результат по домену в вехе родительского объекта комплекта. Номер вехи читается из milestone_id родителя и не повторяется в каждой строке. ID результата сохраняется.'
    id: Annotated[int, Field(ge=1)]
    domain: str


class Strategy(BaseModel):
    id: Annotated[str, Field(min_length=1)]
    protocol: str
    args: Annotated[str, Field(description='При archive_available=true — переносимая строка, совпадающая со strategy.args без завершающего LF; файловые пути относятся к корню ZIP. Одной строки недостаточно при наличии зависимостей: полный состав даёт archive. При archive_available=false — прежние сохранённые аргументы для просмотра, без обещания готового переносимого состава. Неполный legacy-результат не преобразуется догадкой.')]
    family: str
    archive_available: Annotated[bool, Field(description='Полный состав доступен через GET /data/strategies/{id}/archive. Использовать id этой сущности; внутренний путь не выдаётся. Для legacy неизвестная совместимость полного состава с nfqws2 также означает false; автоматической конвертации нет.')]
    milestone_id: Annotated[int, Field(ge=0)]
    results: list[Result]
    family_rank: Annotated[int | None, Field(description='Вычисляемый порядок типа из общего справочника, не оценка качества. Неизвестный тип — null, при группировке в конце.')]
    family_reason: Annotated[str, Field(description='Вычисляемая подсказка типа; неизвестный — явное сообщение об отсутствии описания. Не оценка риска.')]


class StrategyPage(BaseModel):
    items: list[Strategy]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]
    milestone_id: Annotated[int, Field(ge=0)]


class BundleSource(BaseModel):
    source: str
    name: str | None = Field(default_factory=lambda: cast(Any, None))
    reference: Annotated[str | None, Field(description='Известная ссылка на исходник комплекта; если известна точная ссылка на конкретную редакцию, используется она. Неизвестная ссылка не выдумывается.')] = Field(default_factory=lambda: cast(Any, None))


class Bundle(BaseModel):
    id: Annotated[int, Field(ge=1)]
    name: str | None
    sources: list[BundleSource]
    archive_available: Annotated[bool, Field(description='Полный состав доступен через GET /data/bundles/{id}/archive. Использовать id этой сущности; внутренний путь не выдаётся. Для legacy неизвестная совместимость полного состава с nfqws2 также означает false; автоматической конвертации нет.')]
    milestone_id: Annotated[int, Field(ge=0)]
    results: list[BundleResult]


class BundlePage(BaseModel):
    items: list[Bundle]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]
    milestone_id: Annotated[int, Field(ge=0)]


class DomainListSource1(BaseModel):
    code: Annotated[str, Field(min_length=1, description='Известный код первоначального внешнего источника, например v2fly. Сам по себе не означает, что источник сейчас установлен или поддерживается.')]
    categories: Annotated[list[Annotated[str, Field(min_length=1)]], Field(description='Известные категории первоначального источника; пустой массив, если категории не известны или не применимы.', json_schema_extra={'uniqueItems': True})]


class DomainList(BaseModel):
    'Метаданные списка. Обращение к списку — по id; name — название для пользователя. Отдельного публичного label нет. Внутреннее разделение finder/common не возвращается; списки адресуются по id независимо от движка.'
    id: Annotated[int, Field(ge=1, description='Постоянный идентификатор списка. Переименование не меняет id.')]
    name: Annotated[str, Field(description='Единственное публичное, отображаемое название списка. В существующем хранилище читается непустой label, иначе name; внутреннее имя-ключ отдельно не возвращается.', json_schema_extra={'example': 'Видеосервисы'})]
    kind: Annotated[Literal['builtin', 'user'], Field(description='Тип списка. builtin — встроенный: чтение и использование в поиске; user — пользовательский: также изменение и удаление. Тип определяет Core, клиент при создании/замене его не задаёт. Существующее внутреннее значение system отображается как builtin, user — как user.', json_schema_extra={'example': 'user'})]
    source: Annotated[DomainListSource1 | None, Field(description='Происхождение списка: источник и известные категории. Если достоверного внешнего происхождения нет, null. Это справка о первоначальном заимствовании, не автоматическая синхронизация и не гарантия совпадения с источником после ручных изменений. Внутренние пути, base_url и служебные даты не выдаются в этом объекте.', examples=[{'code': 'v2fly', 'categories': ['youtube', 'discord']}, None])]


class DomainListPage(BaseModel):
    items: list[DomainList]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]


class DomainEntry(BaseModel):
    'Элемент готового набора доменов. Порядок элементов передаётся порядком массива; скрытого поднабора нет.'
    domain: str


class DomainPage(BaseModel):
    items: list[DomainEntry]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]


class DomainListWriteDomainsItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    domain: Annotated[str, Field(min_length=1)]


class DomainListWrite(BaseModel):
    'Создать или заменить весь пользовательский набор. Элемент содержит только domain; порядок задаётся порядком массива. enabled не принимается. Для создания и замены списка действует одно правило: дубли доменов после общей нормализации — 400 invalid_parameters. Домены не удаляются из входа молча; при отказе новый список не создаётся, существующий не изменяется.'
    model_config = ConfigDict(extra='forbid', strict=True)
    name: Annotated[str, Field(min_length=1, description='Отображаемое название списка; при замене списка его id сохраняется. Отдельное поле label не принимается.', json_schema_extra={'example': 'Видеосервисы'})]
    domains: list[DomainListWriteDomainsItem]


class Source(BaseModel):
    source: str
    state: Literal['missing', 'ready', 'error']
    installed_revision: str | None
    error: Error | None


class SourceUpdate(BaseModel):
    source: str
    installed_revision: str | None
    available_revision: str | None
    update_available: bool


class Category(BaseModel):
    id: str
    name: str


class CategoryPage(BaseModel):
    items: list[Category]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]


class SourceDomainPage(BaseModel):
    items: list[str]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=200)]


class Backup(BaseModel):
    created_at: Annotated[str, Field(json_schema_extra={'format': 'date-time'})]
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern='^[a-f0-9]{64}$')]


class RestoreResult(BaseModel):
    restored: Literal[True]
    warnings: Annotated[list[Error], Field(description='Дополнительные технические предупреждения при состоявшемся восстановлении, например temporary_cleanup_failed. Не подменяют restored.')] = Field(default_factory=lambda: cast(Any, None))


class CoreStatus(BaseModel):
    'busy=false означает operation=null; busy=true требует конкретный вид операции. Новые незнакомые поля ответа допустимы.'
    model_config = ConfigDict(json_schema_extra={'allOf': [{'if': {'properties': {'busy': {'const': False}}, 'required': ['busy']}, 'then': {'properties': {'operation': {'type': 'null'}}}, 'else': {'properties': {'operation': {'type': 'string'}}}}]})
    core_version: str
    api_version: Literal['2']
    storage: Literal['ready', 'error']
    busy: bool
    operation: Literal['search', 'backup_create', 'backup_restore', 'archive_download', 'program_download', 'core_update', 'source_update'] | None
    backup_exists: Annotated[bool, Field(description='Есть ли сохранённая резервная копия: true — есть, false — нет. Незавершённый временный файл копией не считается. Это наличие копии, не подтверждение её целостности или возможности восстановления прямо сейчас.', examples=[False, True])]


class Release(BaseModel):
    version: str
    tag: str
    prerelease: bool


class ReleaseList(BaseModel):
    channel: Literal['stable', 'prerelease']
    items: list[Release]


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Annotated[str, Field(min_length=1, description='Точное Release.version из списка, не произвольный tag/URL. API установки не допускает понижение по нормализованной версии пакета PEP440.')]


class Download(BaseModel):
    version: str
    size_bytes: Annotated[int, Field(ge=0)]


class Update(BaseModel):
    update_id: Annotated[str, Field(min_length=1, pattern='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', json_schema_extra={'format': 'uuid'})]
    target_version: str
    state: Literal['running', 'succeeded', 'failed', 'unknown']
    error: Error | None


class ExportManifestFilesItem(BaseModel):
    path: str
    sha256: Annotated[str, Field(pattern='^[a-f0-9]{64}$')]


class ExportManifest(BaseModel):
    'manifest.json внутри ZIP. Рабочий состав — ZIP. Единственный источник исполняемых аргументов внутри ZIP — entrypoint (strategy.args либо bundle.conf): UTF-8, лексические POSIX shell words, без выполнения shell, подстановок, glob и комментариев. Порядок argv сохраняется. Все файловые аргументы nfqws2 ссылаются на пути из manifest.files относительно корня ZIP; @ в --blob=name:@path и --lua-init=@path — часть синтаксиса nfqws2, не пути. Все файлы manifest покрывают полный рабочий состав, включая entrypoint; manifest.json не включает сам себя. Сверка SHA-256 выполняется до использования. Пример и экранирование — api-v2-contract.md §5.1. Потребитель выбирает размещение на устройстве и заменяет только файловые аргументы, а не произвольные совпадения текста. В manifest нет ID каталога. README описывает состав и синтаксис ссылок; инструкции изменения роутера не входят в обязанности Core. Формат пользовательского backup не меняется.'
    model_config = ConfigDict(json_schema_extra={'allOf': [{'if': {'properties': {'kind': {'const': 'strategy'}}, 'required': ['kind']}, 'then': {'properties': {'entrypoint': {'const': 'strategy.args'}}}, 'else': {'properties': {'entrypoint': {'const': 'bundle.conf'}}}}]})
    schema_version: Literal[1]
    kind: Literal['strategy', 'bundle']
    target: Literal['nfqws2']
    entrypoint: Annotated[Literal['strategy.args', 'bundle.conf'], Field(description='strategy.args для kind=strategy; bundle.conf для kind=bundle. Путь от корня ZIP; файл указан в files со своей SHA-256.')]
    files: list[ExportManifestFilesItem]


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Annotated[str, Field(min_length=1, description='Точное Release.version; не произвольный tag/URL.')]
    update_id: Annotated[str, Field(description='Уникальный ID, созданный и сохранённый клиентом до отправки. Записывается Core до запуска installer, нужен для чтения после потери 202.', pattern='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', json_schema_extra={'format': 'uuid'})]


class IdleSearch(BaseModel):
    model_config = ConfigDict(extra='allow')
    active: Literal[False]
    run: None


class ActiveSearchRun(RunStatus):
    status: Literal['starting', 'running', 'stopping'] = Field(default_factory=lambda: cast(Any, None))
    engine: Engine = Field(default_factory=lambda: cast(Any, None))


class ActiveSearch(BaseModel):
    model_config = ConfigDict(extra='allow')
    active: Literal[True]
    run: ActiveSearchRun


def _exclusive_search_states(schema: dict[str, Any]) -> None:
    # The branches are distinguished by the boolean active flag.
    schema["oneOf"] = schema.pop("anyOf")


class CurrentSearch(RootModel[IdleSearch | ActiveSearch]):
    'Единственный активный поиск независимо от движка. После окончания run=null; итог читается по сохранённому ID в Данных. В живом статусе finished_at отсутствует; прогноз окончания не рассчитывается.'
    model_config = ConfigDict(json_schema_extra=_exclusive_search_states)


class StopSearchResultRun1(RunStatus):
    status: Literal['stopped', 'completed', 'failed'] = Field(default_factory=lambda: cast(Any, None))
    finished_at: Annotated[str, Field(json_schema_extra={'format': 'date-time'})] | None


class StopSearchResult(BaseModel):
    'Фактический итог остановки захваченного текущего запуска; null, если в момент приёма команды поиска не было. finished_at — фактическое время окончания, не прогноз; формат и допустимость неизвестного времени сохраняются.'
    model_config = ConfigDict(extra='allow')
    run: StopSearchResultRun1 | None


class SearchAccepted(BaseModel):
    'Краткое подтверждение принятия start/resume после проверок и атомарного допуска. Только ID и фактическое состояние на момент ответа, обычно starting; это не обещание успешного завершения. Подробности доступны через общий статус/SSE и запись в Данных.'
    model_config = ConfigDict(extra='allow')
    id: Annotated[str, Field(min_length=1)]
    status: Literal['starting', 'running', 'stopping', 'stopped', 'completed', 'failed']


class UpdateAccepted(Update):
    state: Literal['running']
