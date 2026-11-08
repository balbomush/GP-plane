from __future__ import annotations

import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    def test_current_installation_documents_exact_identity_and_explicit_clean(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts/bootstrap-linux.sh").read_text(encoding="utf-8")
        self.assertIn("API 2 пока нет отдельного", readme)
        self.assertIn("GP_INSTALL_WEB=off", readme)
        self.assertNotIn("GP_INSTALL_WEB=on", readme)
        self.assertIn('--candidate-sha "$(git rev-parse HEAD)" --web off --mode ordinary', readme)
        self.assertIn("`--mode clean` явно удаляет прежнее состояние Core", readme)
        self.assertIn("BASE_URL=http://127.0.0.1:8081", readme)
        self.assertIn("/api/v2/service/updates/{update_id}", readme)
        for forbidden in ("/releases/download/v0.4.4/", "latest-stable", "--initial-install",
                          "/api/core/clean-install-vaults", "/api/auth/login"):
            self.assertNotIn(forbidden, readme)
        self.assertIn("exact annotated release tag", bootstrap)
        self.assertNotRegex(readme, r"GP_BRANCH=v\d+\.\d+\.\d+-alpha\.\d+")


    def test_historical_api1_release_documentation_preserves_its_boundary(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = (root / "docs" / "headless-runtime-core-api.md").read_text(encoding="utf-8")

        self.assertEqual(1, document.count("GET /api/service/releases/available"))
        self.assertIn("exact annotated\nrelease tag в `GP_BRANCH`", document)
        self.assertIn("До остановки прежней GP installer завершает подготовку", document)
        self.assertIn("устанавливает уже проверенные wheels без сети", document)
        self.assertIn("до `domain-sources prepare-v2fly` и до любого `systemctl enable --now`", document)
        for retired_operation in (
            "GET /api/service/releases/install-channel",
            "POST /api/service/releases/set-install-channel",
            "GET /api/service/releases/install-plan",
            "POST /api/service/releases/install",
        ):
            with self.subTest(retired_operation=retired_operation):
                self.assertNotIn(retired_operation, document)


if __name__ == "__main__":
    unittest.main()
