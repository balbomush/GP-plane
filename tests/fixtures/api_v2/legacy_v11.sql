BEGIN TRANSACTION;
CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        );
INSERT INTO "app_settings" VALUES('auth','{"username":"admin","password_salt":"vO441xo-mXr0SM2ovHMQTrzT1hMhpOFD","password_hash":"5e5ceec7f928934dfa64b4fc1ef0b9628ecd6ce731516caea31e172ececd6c1c","token_secret":"pvkJhDIjZ788XyGU-0yQLxM_Huc8VbspJfCtKvIlGR8","token_version":2}','2026-11-08T19:06:34Z');
INSERT INTO "app_settings" VALUES('run_settings','{"parallel":9}','2026-10-28T09:00:00Z');
CREATE TABLE domain_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user',
            label TEXT NOT NULL DEFAULT '',
            source_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope, name, kind)
        );
INSERT INTO "domain_presets" VALUES(1,'finder','required','system','required','{"type":"system"}');
INSERT INTO "domain_presets" VALUES(2,'finder','desired','system','desired','{"type":"system"}');
INSERT INTO "domain_presets" VALUES(5,'custom','sample','user','','{}');
INSERT INTO "domain_presets" VALUES(6,'system','sample','system','','{}');
CREATE TABLE domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            service_group TEXT NOT NULL DEFAULT ''
        );
INSERT INTO "domains" VALUES(3,'one.example','');
INSERT INTO "domains" VALUES(7,'two.example','');
CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
INSERT INTO "meta" VALUES('strategy_analysis_backfilled_v10','1');
INSERT INTO "meta" VALUES('legacy_storage_removed_started_v9','1');
INSERT INTO "meta" VALUES('legacy_storage_removed','1');
INSERT INTO "meta" VALUES('legacy_storage_removed_v9','1');
INSERT INTO "meta" VALUES('legacy_storage_removed_tables','0');
INSERT INTO "meta" VALUES('run_payloads_compaction_started_v7','1');
INSERT INTO "meta" VALUES('run_payloads_compacted_v7','1');
INSERT INTO "meta" VALUES('run_payloads_compacted_count','0');
INSERT INTO "meta" VALUES('run_payloads_original_bytes','0');
INSERT INTO "meta" VALUES('run_payloads_compact_bytes','0');
INSERT INTO "meta" VALUES('run_payloads_compaction_completed_v7','1');
INSERT INTO "meta" VALUES('strategy_attempts_removed_v9','1');
INSERT INTO "meta" VALUES('strategy_attempts_removed_count','0');
INSERT INTO "meta" VALUES('schema_version','11');
INSERT INTO "meta" VALUES('runtime_state_cleaned_v7','1');
INSERT INTO "meta" VALUES('jobs_jsonl_compacted_v7','1');
CREATE TABLE preset_domains (
            preset_id INTEGER NOT NULL,
            domain_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(preset_id, domain_id),
            FOREIGN KEY(preset_id) REFERENCES domain_presets(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
INSERT INTO "preset_domains" VALUES(5,3,-2,0);
INSERT INTO "preset_domains" VALUES(5,7,0,1);
INSERT INTO "preset_domains" VALUES(6,7,0,1);
CREATE TABLE runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL
        );
INSERT INTO "runs" VALUES(1,'kept','discover','running','','{"discovery_options":{"parallel":2},"domains":["one.example"],"id":"kept","kind":"discover","started_at":"2026-10-28T09:00:00Z","status":"running","test":"standard"}');
INSERT INTO "runs" VALUES(2,'kept','discover','failed','2026-10-28T09:02:00Z','{"domains":[],"id":"kept","interrupted":true,"interrupted_reason":"service stopped","kind":"discover","status":"failed","test":"standard","timestamp":"2026-10-28T09:02:00Z"}');
INSERT INTO "runs" VALUES(3,'','old-kind','timeout','','{"candidate_id":"unverified","id":"","kind":"old-kind","status":"timeout"}');
INSERT INTO "runs" VALUES(4,'','old-kind','stopped','','{"id":"","kind":"old-kind","status":"stopped"}');
CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
INSERT INTO "schema_migrations" VALUES(1,'base_candidate_storage','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(2,'normalized_domain_strategy_model','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(3,'minimal_backup_model','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(4,'runtime_observability','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(5,'remove_legacy_candidate_storage','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(6,'preset_domain_state','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(7,'compact_runtime_payloads','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(8,'trim_strategy_attempt_diagnostics','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(9,'minimal_sqlite_working_model','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(10,'strategy_analysis_metadata','2026-10-28T09:00:00Z');
INSERT INTO "schema_migrations" VALUES(11,'app_settings','2026-10-28T09:00:00Z');
CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            fragmentation_class TEXT NOT NULL DEFAULT 'unknown',
            fragmentation_safe INTEGER NOT NULL DEFAULT 0,
            fragmentation_reason TEXT NOT NULL DEFAULT '',
            family TEXT NOT NULL DEFAULT 'other',
            family_key TEXT NOT NULL DEFAULT '',
            family_rank INTEGER NOT NULL DEFAULT 900,
            family_reason TEXT NOT NULL DEFAULT ''
        );
INSERT INTO "strategies" VALUES('unverified','tls','--lua-desync=fake:blob=unknown','','candidate','unknown',0,'','fake','',900,'');
INSERT INTO "strategies" VALUES('same-text','tls','--lua-desync=fake:blob=unknown','','candidate','unknown',0,'','fake','',900,'');
INSERT INTO "strategies" VALUES('unknown-protocol','','--unknown','','candidate','unknown',0,'','other','',900,'');
CREATE TABLE strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
INSERT INTO "strategy_domain_results" VALUES('unverified',3,'tls','single_domain');
INSERT INTO "strategy_domain_results" VALUES('unverified',3,'tls','multi_domain');
INSERT INTO "strategy_domain_results" VALUES('same-text',7,'tls','single_domain');
INSERT INTO "strategy_domain_results" VALUES('unknown-protocol',7,'','single_domain');
CREATE INDEX idx_runs_id_seq ON runs(id, seq);
CREATE INDEX idx_runs_seq ON runs(seq);
CREATE INDEX idx_domains_name ON domains(name);
CREATE INDEX idx_domains_service_group ON domains(service_group);
CREATE INDEX idx_strategies_protocol ON strategies(protocol);
CREATE INDEX idx_strategies_args_hash ON strategies(args_hash);
CREATE INDEX idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
CREATE INDEX idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
CREATE INDEX idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
CREATE INDEX idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
CREATE INDEX idx_domain_presets_scope_name ON domain_presets(scope, name);
CREATE INDEX idx_preset_domains_domain ON preset_domains(domain_id);
CREATE INDEX idx_strategies_family ON strategies(family, family_rank);
CREATE INDEX idx_strategies_fragmentation ON strategies(fragmentation_class);
CREATE INDEX idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position);
CREATE INDEX idx_preset_domains_preset_position ON preset_domains(preset_id, position);
CREATE VIEW domain_stats AS
        SELECT d.id AS domain_id,
               d.name AS domain,
               COUNT(DISTINCT r.strategy_id) AS strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'tls' THEN r.strategy_id END) AS tls_strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;
CREATE VIEW strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('domain_presets',6);
INSERT INTO "sqlite_sequence" VALUES('domains',7);
INSERT INTO "sqlite_sequence" VALUES('runs',4);
COMMIT;
