from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    def test_installation_commands_require_an_exact_release_tag(self) -> None:
        """Core-only candidate docs cannot advertise rewritten v0.4.4 assets.

        The unchanged installer suites test stable/alpha/tag/vault execution;
        this assertion updates only the documented topology and candidate stage.
        """
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts/bootstrap-linux.sh").read_text(encoding="utf-8")
        self.assertIn('Новый стабильный выпуск ещё не опубликован', readme)
        self.assertIn('GP_INSTALL_WEB=off', readme)
        self.assertNotIn('GP_INSTALL_WEB=on', readme)
        self.assertNotIn('/releases/download/v0.4.4/bootstrap-linux.sh', readme)
        self.assertIn('--candidate-sha "$(git rev-parse HEAD)" --web off', readme)
        self.assertIn('не являются обычной пользовательской установкой', readme)
        self.assertIn('bootstrap сам проверяет единственный поддержанный source', readme)
        self.assertIn('останавливается до `sudo`', readme)
        self.assertIn('pending vault при живом source', readme)
        self.assertIn('Vault восстанавливается install user', readme)
        self.assertIn('Ручное подтверждение через HTTP/API', readme)
        self.assertIn('/api/core/clean-install-vaults/create', readme)
        self.assertIn('BASE_URL=http://127.0.0.1:8081', readme)
        self.assertIn('`gp-web.service`', readme)
        self.assertIn('Core update его не останавливает и не удаляет', readme)
        self.assertIn('Откат на старый бинарник', readme)
        self.assertIn('Alpha → stable требует отдельной clean-install', readme)
        self.assertIn('внутренний этап подготовки из полного checkout', readme)
        for forbidden in ['/raw/', 'latest-stable', 'GP_INSTALL_CONFIG', 'GP_STATE_DIR', 'GP_ZAPRET_INSTALLER_URL', 'v2fly/domain-list-community']:
            self.assertNotIn(forbidden, readme)
        self.assertIn('TAG="${GP_BRANCH:-}"', bootstrap)
        self.assertIn('exact release tag vX.Y.Z or vX.Y.Z-alpha.N', bootstrap)
        self.assertNotRegex(readme, r"GP_BRANCH=v\d+\.\d+\.\d+-alpha\.\d+")


    def test_release_documentation_keeps_installation_outside_web_and_api(self) -> None:
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
