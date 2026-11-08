"""Selected-result filtering and catalog export over HTTP."""

import hashlib
from io import BytesIO
import json
import shlex
import unittest
from zipfile import ZipFile

from gp_control_plane import storage, strategy_assets
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


PATH = "/api/v2/data/strategies"


class CatalogTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.configured = hasattr(api_v2, "register_catalog")
        if self.configured:
            api_v2.register_catalog(app, self.state)

    def test_filter_order_and_portable_archive(self):
        self.assertTrue(self.configured, "catalog routes are missing")
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        with storage.connect_api2(self.state) as conn:
            conn.executemany(
                """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json)
                   VALUES(?, 1, ?, 'running', '{}', '["one.example","two.example"]')""",
                [("stock", 1), ("gp", 2)],
            )
        path = "/runtime/tls hello.bin"
        args = f"--blob=hello:@'{path}' --filter-tcp=443 --new --lua-desync=fake:blob=hello"
        details = dict(protocol="tls", execution_format="nfqws2", args=args, files={path: b"blob data"})
        strategy = storage.save_strategy_check(
            self.state, run_id="stock", outcomes={"one.example": True, "two.example": False},
            checkpoint=None, **details,
        )
        self.assertEqual(strategy, storage.save_strategy_check(
            self.state, run_id="gp", outcomes={"one.example": False, "two.example": True},
            checkpoint={"index": 1}, **details,
        ))
        query = "?domains=one.example&domains=two.example&engine_code=blockcheck2"
        self.assertEqual(self.call("GET", PATH + query + "&match=all", b"", token)[2]["total"], 0)
        status, _, page = self.call("GET", PATH + query, b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(page["milestone_id"], 1)
        self.assertEqual(page["total"], 1)
        view = page["items"][0]
        self.assertEqual(view["id"], strategy)
        self.assertEqual([r["domain"] for r in view["results"]], ["one.example"])
        self.assertEqual([e["code"] for e in view["results"][0]["engines"]], ["blockcheck2"])
        self.assertNotIn("fingerprint", view)
        status, headers, data = self.call("GET", f"{PATH}/{strategy}/archive", b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with ZipFile(BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["target"], "nfqws2")
            entry = archive.read(manifest["entrypoint"]).decode().rstrip("\n")
            self.assertEqual(entry, view["args"])
            words = shlex.split(entry)
            self.assertEqual(words[2], "--new")
            self.assertEqual(archive.read(words[0].split("@", 1)[1]), b"blob data")
            for item in manifest["files"]:
                self.assertEqual(hashlib.sha256(archive.read(item["path"])).hexdigest(), item["sha256"])
        with storage.connect_api2(self.state) as conn:
            reference = conn.execute("SELECT assets_ref FROM strategies WHERE id=?", (strategy,)).fetchone()[0]
            conn.execute("INSERT INTO strategies(id, protocol, args, family) VALUES('legacy', 'tls', 'known', 'other')")
        blob = self.state / "strategy-finder" / "assets" / reference / "assets" / "0.bin"
        blob.write_bytes(b"damaged")
        self.assertFalse(self.call("GET", f"{PATH}/{strategy}", b"", token)[2]["archive_available"])
        self.assertEqual(self.call("GET", f"{PATH}/{strategy}/archive", b"", token)[0], 409)
        status, _, body = self.call("GET", PATH + "/legacy/archive", b"", token)
        self.assertEqual((status, body["error"]["code"]), (409, "archive_incomplete"))

    def test_bundle_with_two_sources_is_exportable_without_current_confirmation(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        files = {"lua script": b"lua", "host list": b"test.example\n"}
        args = "--lua-init=@'lua script' --filter-tcp=443 --hostlist='host list' --new --filter-udp=443"
        composition = strategy_assets.capture("nfqws2", "tls", args, files)
        strategy_assets.persist(self.state / "strategy-finder" / "assets", composition)
        sources = [{"source": "fixture1", "name": "One", "reference": None},
                   {"source": "fixture2", "name": "Two", "reference": "test"}]
        with storage.connect_api2(self.state) as conn:
            conn.execute(
                "INSERT INTO bundles VALUES(7, ?, 'demo', ?, ?)",
                (composition.fingerprint, json.dumps(sources), composition.fingerprint),
            )
        path = "/api/v2/data/bundles"
        self.assertEqual(self.call("GET", path, b"", token)[2]["total"], 0)
        listed = self.call("GET", path + "?view=all", b"", token)[2]["items"]
        status, _, view = self.call("GET", path + "/7", b"", token)
        self.assertEqual((status, listed), (200, [view]))
        self.assertEqual(view["sources"], sources)
        self.assertEqual(view["results"], [])
        self.assertNotIn("fingerprint", view)
        status, _, payload = self.call("GET", path + "/7/archive", b"", token)
        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(archive.read(manifest["entrypoint"]).decode().strip(), composition.args)
            for name, data in composition.files:
                self.assertEqual(archive.read("assets/" + name), data)
