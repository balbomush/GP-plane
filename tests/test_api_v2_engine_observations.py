"""Only complete engine observation groups reach the strategy transaction."""

from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gp_control_plane.engines.api2_blockcheck import _Observations
from gp_control_plane.jobs import ManagedRuntimeQuarantinedError


class EngineObservationsTests(unittest.TestCase):
    def test_only_observed_table_is_removed_and_unconfirmed_removal_quarantines(self):
        observer = _Observations(SimpleNamespace(remember_nft_table=lambda table: None), None, "fixture")
        observer.record_line("GP-API2 fixture table blockcheck123\n")
        with patch("gp_control_plane.zapret2._owned_nft_present", return_value=True) as inspect, patch(
            "gp_control_plane.zapret2._run_recovery_root_helper",
        ) as helper:
            with self.assertRaises(ManagedRuntimeQuarantinedError) as failure:
                observer.confirm_cleanup()
            self.assertEqual(failure.exception.nft_table, "blockcheck123")
            helper.assert_called_once_with(["nft-delete-blockcheck-table", "inet", "blockcheck123"])
            self.assertEqual([call.args for call in inspect.call_args_list], [("blockcheck123",)] * 2)
        with patch("gp_control_plane.zapret2._owned_nft_present", side_effect=[True, False]), patch(
            "gp_control_plane.zapret2._run_recovery_root_helper",
        ) as helper:
            observer.confirm_cleanup()
            helper.assert_called_once_with(["nft-delete-blockcheck-table", "inet", "blockcheck123"])

    def test_stock_combines_exact_strategy_across_domains_and_gp_rejects_partial_end(self):
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "hello world.bin").write_bytes(b"fixture dependency")
            words = b64encode(b"--filter-tcp=443\0--blob=demo:@hello world.bin\0").decode()
            recorded = []
            job = SimpleNamespace(
                code="blockcheck2", domains=["one.example", "two.example"],
                check=lambda *args: recorded.append(args), log=lambda line: None,
            )
            observed = _Observations(job, base, "fixture")
            begin = f"GP-API2 fixture begin {'a' * 64} curl_test_https_tls12 {words}\n"
            observed.record_line(begin)
            observed.record_line("GP-API2 fixture domain one.example 0\n")
            self.assertEqual(recorded, [])
            observed.record_line("GP-API2 fixture end\n")
            self.assertEqual(recorded, [])
            observed.record_line(begin)
            observed.record_line("GP-API2 fixture domain two.example 7\n")
            observed.record_line("GP-API2 fixture end\n")
            self.assertEqual(recorded[0][3], {"hello world.bin": b"fixture dependency"})
            self.assertEqual(recorded[0][4], {"one.example": True, "two.example": False})
            job.code = "blockcheck2-gp"
            observed.record_line(begin)
            observed.record_line("GP-API2 fixture domain one.example 0\n")
            with self.assertRaisesRegex(ValueError, "all selected domains"):
                observed.record_line("GP-API2 fixture end\n")
            self.assertEqual(len(recorded), 1)
