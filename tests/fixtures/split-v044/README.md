# v0.4.4 split baseline fixtures

Product source: balbomush/GP-access-control-plane,
commit 66b5898d6c30544df00d928126ecc5a38ea6fffc (released v0.4.4).
License/provenance: existing GP MIT LICENSE and THIRD_PARTY_NOTICES apply.

openapi.json is an unchanged copy of that source specification. inventory.json
records source-derived public RouteSpec methods, Core internal view mappings,
resource limits, literal actions, dataset handlers and HTML controls with
source locations. These are preparation fixtures, not a claim that every
control or hardware operation was exercised. For docs/non-API paths the
effective auth policy differs from the RouteSpec default flag.

tests/browser/split_baseline.cjs supplements existing R1/R2 tests against a
real installed released Core/Web pair on loopback with synthetic state only.
Arguments: WEB_URL PINNED_PLAYWRIGHT_RUNTIME NEW_EVIDENCE_DIRECTORY.
It checks login/401, shell navigation, real persisted presets/SSE, backup
create/download/restore/delete, password rotation/relogin/logout. No engine,
helper or real vault qualification is claimed. Browser outputs are local
evidence and must not be committed. No user credentials or backups are used.

Keep baseline expectations unchanged when migrating UI. Any genuine source
defect is a separate finding; do not encode it silently as desired behavior.
