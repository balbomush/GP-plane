from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path


OPENAPI_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SWAGGER_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
SWAGGER_PATHS = {"/swagger", "/swagger/"}
OPENAPI_OPERATION_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
CORE_ONLY_OPENAPI_INFO = {
    "title": "GP Control Plane Core API",
    "description": (
        "Callable Core/Service/OpenAPI operations for the headless GP runtime. "
        "This core-only contract contains registered /api/core, /api/service and /openapi.json routes."
    ),
}


def _openapi_source_bytes() -> bytes:
    resource = files("gp_control_plane.http").joinpath("openapi.json")
    if resource.is_file():
        return resource.read_bytes()
    # Source checkout only; never search cwd or ancestors of site-packages.
    module = Path(__file__).resolve()
    if module.parent.name == "http" and module.parents[1].name == "gp_control_plane" and module.parents[2].name == "src":
        root = module.parents[3]
        if (root / "pyproject.toml").is_file():
            return (root / "openapi.json").read_bytes()
    raise FileNotFoundError("packaged OpenAPI contract is unavailable")


def openapi_json_bytes(*, core_only: bool = False) -> bytes:
    data = _openapi_source_bytes()
    if not core_only:
        return data

    contract = json.loads(data.decode("utf-8"))

    from .routes import openapi_operations

    contract["info"] = {**contract.get("info", {}), **CORE_ONLY_OPENAPI_INFO}
    allowed = openapi_operations(core_only=True)
    contract["paths"] = {
        path: {
            name: value
            for name, value in path_item.items()
            if name.lower() not in OPENAPI_OPERATION_METHODS or (path, name.upper()) in allowed
        }
        for path, path_item in contract.get("paths", {}).items()
        if any((path, method.upper()) in allowed for method in path_item)
    }
    used_tags = {
        tag
        for operations in contract["paths"].values()
        for method, operation in operations.items()
        if method.lower() in OPENAPI_OPERATION_METHODS
        for tag in operation.get("tags", [])
    }
    contract["tags"] = [tag for tag in contract.get("tags", []) if tag.get("name") in used_tags]
    return json.dumps(contract, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GP Control Plane API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; background: #f8fafc; }
    .topbar { display: none; }
    .swagger-ui .info { margin: 24px 0; }
    .swagger-ui .scheme-container { box-shadow: none; border: 1px solid #e2e8f0; }
    .swagger-ui .wrapper { max-width: 1280px; }
    .offline {
      margin: 24px;
      padding: 16px 18px;
      border: 1px solid #fecaca;
      border-radius: 8px;
      background: #fff1f2;
      color: #7f1d1d;
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .offline a { color: #991b1b; font-weight: 600; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <noscript>
    <div class="offline">Для Swagger UI нужен JavaScript. Raw OpenAPI доступен по адресу <a href="/openapi.json">/openapi.json</a>.</div>
  </noscript>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.addEventListener('load', () => {
      if (!window.SwaggerUIBundle || !window.SwaggerUIStandalonePreset) {
        document.getElementById('swagger-ui').innerHTML =
          '<div class="offline">Не удалось загрузить Swagger UI. Raw OpenAPI доступен по адресу <a href="/openapi.json">/openapi.json</a>.</div>';
        return;
      }
      window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
        displayRequestDuration: true,
        docExpansion: 'list',
        defaultModelsExpandDepth: 1,
        persistAuthorization: true,
        tryItOutEnabled: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        layout: 'BaseLayout'
      });
    });
  </script>
</body>
</html>
"""
