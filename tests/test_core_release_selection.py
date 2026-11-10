"""RS-01–09: stage selection and immutable downloaded supply binding."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gp_control_plane import core_supply, releases
from tests.test_api_v2_service import DELIVERY, WHEEL


def row(identifier=1, stage="alpha", published="2026-11-09T12:00:00Z", **changes):
    return {"id": identifier, "tag_name": f"v0.5.0-alpha.{identifier}",
            "published_at": published, "draft": False, "prerelease": stage != "stable",
            "body": f"<!-- gp-release-stage: {stage} -->",
            "assets": [{"name": name, "size": len(data)} for name, data in DELIVERY.items()], **changes}


class ReleaseSelectionTests(unittest.TestCase):
    def test_incomplete_legacy_version_does_not_hide_other_versions_in_stage_list(self):
        legacy = row(1, 'stable', tag_name='v0.4.4', assets=[], body='')
        ready = row(2, 'stable')
        with self.catalog([legacy, ready]):
            self.assertEqual(core_supply.list_releases('stable'), [
                {'version':'0.5.0','tag':'stable'}, {'version':'0.4.4','tag':'stable'}])
            with self.assertRaises(ValueError):
                core_supply.latest('0.4.4','stable')
        with self.catalog([legacy, ready, row(3, 'stable', published='2026-11-10T12:00:00Z', assets=[])]):
            self.assertEqual(len(core_supply.list_releases('stable')), 2)
            with self.assertRaises(ValueError):
                core_supply.latest('0.5.0','stable')

    def test_catalog_fetches_all_pages_before_stage_selection(self):
        pages = [json.dumps([{"draft": True}] * 100).encode(), json.dumps([row(2)]).encode()]
        with patch.object(releases, "get_bytes", side_effect=pages) as network:
            self.assertEqual(core_supply.latest("0.5.0", "alpha")[0]["release_id"], 2)
            self.assertIn("page=2", network.call_args_list[1].args[0])

    def test_concurrent_preparations_never_move_a_valid_pinned_bundle(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        original = Path.rename
        moved = []
        def fetch(tag, name, size):
            if name == WHEEL:
                barrier.wait(5)
            return DELIVERY[name]
        def rename(path, target):
            if path.name.startswith("1-") and path.is_dir():
                moved.append(path.name)
            return original(path, target)
        with TemporaryDirectory() as root, self.catalog([row()]), patch.object(
            releases, "fetch_release_asset", side_effect=fetch
        ), patch.object(Path, "rename", rename), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(core_supply.prepare, Path(root), "0.5.0", "alpha") for _ in range(2)]
            supplies = [future.result(10) for future in futures]
            self.assertEqual(supplies[0], supplies[1])
            self.assertTrue(core_supply.pinned_path(Path(root), "0.5.0", "alpha", supplies[0]).is_dir())
            self.assertEqual(moved, [], "A valid pinned bundle must never disappear during another preparation")

    def catalog(self, rows):
        return patch.object(releases, "fetch_github_releases", return_value=json.dumps(rows))

    def test_pub01_latest_pair_order_time_and_id_not_array_or_git_suffix(self):
        old = row(tag_name="v0.5.0", body="")
        newest = row(3, published="2026-11-10T12:00:00Z")
        with self.catalog([old, row(9, "stable"), row(2), newest, row(10, draft=True)]):
            selected = core_supply.latest("0.5.0", "alpha")
            self.assertEqual(selected[0]["release_id"], 3)
            self.assertEqual(core_supply.list_releases("alpha"), [{"version": "0.5.0", "tag": "alpha"}])
        with self.catalog([row(2), row(1)]):
            self.assertEqual(core_supply.latest("0.5.0", "alpha")[0]["release_id"], 2)
        promoted = row(3, "stable")
        with self.catalog([promoted]):
            self.assertIsNone(core_supply.latest("0.5.0", "alpha"))
            self.assertEqual(core_supply.latest("0.5.0", "stable")[0]["git_tag"], "v0.5.0-alpha.3")

    def test_latest_incomplete_or_untrusted_metadata_never_falls_back(self):
        for changed in (dict(assets=[]), dict(published_at="bad"), dict(id=None),
                        dict(body="<!-- gp-release-stage: stable -->"),
                        dict(body="<!-- gp-release-stage: alpha -->\n<!-- gp-release-stage: alpha -->")):
            with self.subTest(changed=changed), self.catalog([row(), row(2, **changed)]):
                with self.assertRaises(ValueError):
                    core_supply.latest("0.5.0", "alpha")

    def test_explicit_download_refreshes_but_install_remains_offline_pinned(self):
        with TemporaryDirectory() as root:
            state = Path(root)
            legacy = state / "strategy-finder/downloads/0.5.0"
            legacy.mkdir(parents=True)
            (legacy / "old-owned-file").write_text("keep")
            with self.catalog([row()]), patch.object(releases, "fetch_release_asset", side_effect=lambda t,n,s: DELIVERY[n]):
                first = core_supply.prepare(state, "0.5.0", "alpha")
            with self.catalog([row(2)]), patch.object(releases, "fetch_release_asset", side_effect=lambda t,n,s: DELIVERY[n]):
                second = core_supply.prepare(state, "0.5.0", "alpha")
            self.assertNotEqual(first["directory"], second["directory"])
            with patch.object(releases, "fetch_github_releases", side_effect=OSError("offline")) as network:
                self.assertEqual(core_supply.for_update(state, "0.5.0", "alpha"), second)
                self.assertTrue(core_supply.pinned_path(state, "0.5.0", "alpha", first).is_dir())
                network.assert_not_called()
            self.assertEqual((legacy / "old-owned-file").read_text(), "keep")
            with self.catalog([row(3)]), patch.object(releases, "fetch_release_asset", return_value=b"damaged"):
                with self.assertRaises(ValueError):
                    core_supply.prepare(state, "0.5.0", "alpha")
            with patch.object(releases, "fetch_github_releases", side_effect=OSError("offline")):
                self.assertEqual(core_supply.for_update(state, "0.5.0", "alpha"), second)
            broken = deepcopy(second)
            broken["directory"] = "../../foreign"
            with self.assertRaises(ValueError):
                core_supply.pinned_path(state, "0.5.0", "alpha", broken)
            (core_supply.pinned_path(state, "0.5.0", "alpha", first) / WHEEL).write_bytes(b"damage")
            with self.assertRaises(ValueError):
                core_supply.pinned_path(state, "0.5.0", "alpha", first)

    def test_invalid_stage_is_not_stable(self):
        for stage in ("", "beta", "v0.5.0-alpha.2", None):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                core_supply.latest("0.5.0", stage)
