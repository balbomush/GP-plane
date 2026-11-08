# API 2 migration fixtures

`legacy_v11.sql` and `legacy_backup_v7.zip` come from the actual storage and
backup writers at Core `f2d5bebd22d30a75181112e3f7abd3660d81074d`.
To regenerate, run `generate_v11.py` with that checkout's `src` on
`PYTHONPATH` and this directory as its argument. The password
`migration-fixture-only` is synthetic. Its salt and token secret are generated
per run. The archive timestamp and hashes may therefore vary on regeneration.

The database contains repeated run snapshots, an interrupted search with
synthetic recovery defaults, two separate missing IDs, a legacy timeout, a
disabled preset member at negative position, an unknown protocol, and two
unverified definitions with the same text. Tests derive malformed cases
from this fixture so the source dump remains a valid v11 input.
