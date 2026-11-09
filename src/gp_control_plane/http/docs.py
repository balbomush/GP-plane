"""Offline Swagger UI for Core API 2."""

def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GP Control Plane API</title>
  <link rel="stylesheet" href="/swagger/assets/swagger-ui.css">
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
  <script src="/swagger/assets/swagger-ui-bundle.js"></script>
  <script src="/swagger/assets/swagger-ui-standalone-preset.js"></script>
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
