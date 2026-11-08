"""API 2 schema statements, executed within the caller's transaction."""

UNCHANGED_TABLES = (
    """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        service_group TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS domain_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'user',
        label TEXT NOT NULL DEFAULT '',
        source_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(scope, name, kind)
    )""",
)

TABLES = (
    """CREATE TABLE meta (
        key TEXT PRIMARY KEY NOT NULL,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE milestones (
        id INTEGER PRIMARY KEY CHECK(id >= 0)
    )""",
    """CREATE TABLE engines (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL
    )""",
    """CREATE TABLE strategies (
        id TEXT PRIMARY KEY NOT NULL,
        protocol TEXT NOT NULL,
        args TEXT NOT NULL,
        fingerprint TEXT UNIQUE,
        family TEXT NOT NULL,
        assets_ref TEXT
    )""",
    """CREATE TABLE strategy_domain_results (
        id INTEGER PRIMARY KEY,
        strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        domain_id INTEGER NOT NULL REFERENCES domains(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        UNIQUE(strategy_id, domain_id, milestone_id)
    )""",
    """CREATE TABLE strategy_result_engines (
        id INTEGER PRIMARY KEY,
        result_id INTEGER NOT NULL REFERENCES strategy_domain_results(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        engine_id INTEGER NOT NULL REFERENCES engines(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        UNIQUE(result_id, engine_id)
    )""",
    """CREATE TABLE bundles (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        name TEXT,
        sources_json TEXT NOT NULL DEFAULT '[]',
        files_ref TEXT NOT NULL
    )""",
    """CREATE TABLE bundle_domain_results (
        id INTEGER PRIMARY KEY,
        bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        domain_id INTEGER NOT NULL REFERENCES domains(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        UNIQUE(bundle_id, domain_id, milestone_id)
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY NOT NULL,
        engine_id INTEGER REFERENCES engines(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        parameters_json TEXT,
        domains_json TEXT,
        preset_id INTEGER REFERENCES domain_presets(id) ON UPDATE RESTRICT ON DELETE SET NULL,
        checkpoint_json TEXT,
        status TEXT NOT NULL,
        error_json TEXT,
        started_at TEXT,
        finished_at TEXT,
        CHECK(milestone_id = 0 OR (
            engine_id IS NOT NULL AND parameters_json IS NOT NULL AND domains_json IS NOT NULL
        ))
    )""",
    """CREATE TABLE preset_domains (
        id INTEGER PRIMARY KEY,
        preset_id INTEGER NOT NULL REFERENCES domain_presets(id) ON UPDATE RESTRICT ON DELETE CASCADE,
        domain_id INTEGER NOT NULL REFERENCES domains(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
        position INTEGER NOT NULL DEFAULT 0 CHECK(typeof(position) = 'integer'),
        UNIQUE(preset_id, domain_id)
    )""",
    """CREATE TABLE access_state (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        token_secret TEXT NOT NULL,
        token_version INTEGER NOT NULL CHECK(typeof(token_version) = 'integer' AND token_version > 0)
    )""",
)

INDEXES = (
    "CREATE INDEX strategy_results_by_domain ON strategy_domain_results(milestone_id, domain_id, strategy_id)",
    "CREATE INDEX bundle_results_by_domain ON bundle_domain_results(milestone_id, domain_id, bundle_id)",
    "CREATE INDEX strategy_results_by_engine ON strategy_result_engines(engine_id, result_id)",
    "CREATE INDEX strategies_by_protocol_family ON strategies(protocol, family, id)",
    "CREATE INDEX runs_by_start ON runs(started_at DESC, id DESC)",
    "CREATE INDEX runs_by_preset ON runs(preset_id)",
    "CREATE INDEX preset_domains_by_domain ON preset_domains(domain_id)",
    "CREATE INDEX preset_domains_by_position ON preset_domains(preset_id, position, domain_id)",
)

ENGINES = ((1, "blockcheck2", "blockcheck2"), (2, "blockcheck2-gp", "blockcheck2 GP"))
