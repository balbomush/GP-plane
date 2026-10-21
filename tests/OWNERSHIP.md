# Test ownership after Core/Web split

Baseline: GP v0.4.4 `66b5898d6c30544df00d928126ecc5a38ea6fffc`;
accepted S2A Core `9a69754d8093fb4b6084d5de88219772284add67`.
The previous mixed UI test files are replaced by the owners below. Their exact
method/assertion provenance and campaign results remain in the local ledger.

| Owner | Checks |
| --- | --- |
| Core `test_core_application_http`, `test_core_auth_http` | Product API, auth/storage, settings, vault, jobs/Stop, recovery and failure/cleanup harness |
| Core `test_core_transport`, `test_core_product_corrections`, `test_core_startup_lifecycle`, `test_core_http_contract` | Headless Bottle/Cheroot, schema/routes, startup/recovery, admission and cleanup |
| Core `test_listener_preparation` | Actual Linux TIME_WAIT versus foreign live listener before activation |
| Core `integration/*` | Separate installed Core/Web processes, HTTP-only product communication; test controller may seed its own synthetic Core state |
| Web `test_a5_web_transport`, `test_product_corrections`, `test_hop_header_contract`, `test_listener_interrupt` | Proxy/body/close/relay, listener workers and native Linux SIGINT |
| Web `test_transport_architecture`, `frontend-*.cjs`, `frontend/*.test.mjs` | Browser transport/session/lifetime, full Svelte UI, unchanged pure selection/run/log logic |
| Web `test_package_contract`, `test_web_contract`, `test_service_ownership` | Delivery, OpenAPI/Swagger, budgets, install compatibility and service ownership |

`qualify_pair.py` requires independently installed environments; a missing or
combined environment is an error. Its report records the exact module subset.
Frontend/browser toolchain is a build/test dependency, never a product runtime
dependency. Linux socket/SIGINT and hardware tests retain their own platform gates.

Old HTML byte hashes, Python renderer cache and legacy DOM/global assertions
belonged to the retired renderer. Delivery failures now exercise packaged Vite
assets and manifest; functional assertions are exercised through Svelte/browser
events. Memory-only bearer handling follows the accepted S2A contract, replacing
the older localStorage persistence expectation. No legacy renderer runs in either
product, and no neighboring product source is imported by a product listener.
