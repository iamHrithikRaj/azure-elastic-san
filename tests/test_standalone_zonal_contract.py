"""The Python downloads intentionally carry the same small in-file contract."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
with (Path(__file__).parent / "standalone_zonal_affinity_cases.json").open(encoding="utf-8") as handle:
    CASES = json.load(handle)


def load_script(platform):
    path = ROOT / ("CLI ({}) Multi-Session Connect Scripts".format(platform)) / "connect_for_documentation.py"
    spec = importlib.util.spec_from_file_location(platform.lower() + "_zonal_contract", path)
    module = importlib.util.module_from_spec(spec)
    if platform == "Linux":
        with mock.patch.object(os.path, "exists", side_effect=lambda value: value == "/usr/bin/apt-get"):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


SCRIPTS = [load_script(platform) for platform in ("Linux", "FreeBSD")]


class SharedZonalContractTests(unittest.TestCase):
    def test_address_fixtures_and_exact_session_assignments(self):
        for script in SCRIPTS:
            for case in CASES["addressCases"]:
                with self.subTest(script=script.__name__, case=case["name"]):
                    if case.get("error"):
                        with self.assertRaises(script.VipResolutionError):
                            script.canonicalize_vips(case["addresses"])
                    else:
                        expected = case["expected"]
                        self.assertEqual(expected, script.canonicalize_vips(case["addresses"]))
                        slots = script.allocate_vip_sessions(case["addresses"])
                        self.assertEqual([expected[i % 3] for i in range(32)], slots)
                        self.assertEqual(CASES["expectedCounts"], [slots.count(ip) for ip in expected])

    def test_mapping_fixtures(self):
        for script in SCRIPTS:
            for case in CASES["mappingCases"]:
                with self.subTest(script=script.__name__, case=case["name"]):
                    args = (case["locations"], case["location"], case["logicalZone"])
                    if case.get("error"):
                        error_type = getattr(script, "ZonalAffinityError", None) or script.ElasticSanConnectError
                        with self.assertRaises(error_type):
                            script.map_logical_to_physical_zone(*args)
                    else:
                        self.assertEqual(case["expected"], script.map_logical_to_physical_zone(*args))

    def test_retry_success_uses_one_answer_and_invocation_cache(self):
        addresses = ["fd00::1", "10.0.0.2", "10.0.0.1"]
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                cache = {}
                with mock.patch.object(script, "_lookup_target_addresses", side_effect=[
                    OSError("temporary failure"), ["10.0.0.8"], addresses,
                ]) as lookup, mock.patch.object(script.time, "sleep") as sleep:
                    first = script.resolve_target_vips("portal.example", cache)
                    self.assertEqual(["10.0.0.1", "10.0.0.2", "fd00::1"], first)
                    first.clear()
                    self.assertEqual(["10.0.0.1", "10.0.0.2", "fd00::1"],
                                     script.resolve_target_vips("PORTAL.example", cache))
                self.assertEqual(3, lookup.call_count)
                self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_rotating_incomplete_answers_are_not_unioned(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                cache = {}
                with mock.patch.object(script, "_lookup_target_addresses", side_effect=[
                    ["10.0.0.1"], ["10.0.0.2"], ["10.0.0.3"],
                ]) as lookup, mock.patch.object(script.time, "sleep"):
                    with self.assertRaisesRegex(script.VipResolutionError, "after 3 attempts"):
                        script.resolve_target_vips("portal.example", cache)
                self.assertEqual(3, lookup.call_count)
                self.assertEqual({}, cache)

    def test_native_resolver_is_bounded_by_subprocess_run(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                result = subprocess.CompletedProcess([], 0, b'["10.0.0.1"]', b"")
                with mock.patch.object(script.subprocess, "run", return_value=result) as run:
                    self.assertEqual(["10.0.0.1"], script._lookup_target_addresses("portal.example"))
                args, kwargs = run.call_args
                self.assertEqual(sys.executable, args[0][0])
                self.assertEqual("portal.example", args[0][-1])
                self.assertEqual(5, kwargs["timeout"])
                self.assertNotIn("shell", kwargs)

    def test_timeout_exhaustion_is_explicit(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                with mock.patch.object(script.subprocess, "run", side_effect=subprocess.TimeoutExpired("DNS", 5)) as run:
                    with mock.patch.object(script.time, "sleep"):
                        with self.assertRaisesRegex(script.VipResolutionError, "after 3 attempts"):
                            script.resolve_target_vips("portal.example")
                self.assertEqual(3, run.call_count)

    def test_strict_count_and_host_port_contract(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                for count in (0, 1, 31, 33, "32"):
                    with self.assertRaises(script.VipResolutionError):
                        script.allocate_vip_sessions(["10.0.0.1", "10.0.0.2", "10.0.0.3"], count)
                self.assertEqual("[fd00::1]:3260", script.format_target_portal("fd00::1", 3260))
                self.assertEqual("10.0.0.1:3260", script.format_target_portal("10.0.0.1", "3260"))
                for port in (0, -1, 65536, True, "3;exit", 3260.5):
                    with self.assertRaises(script.VipResolutionError):
                        script.validate_target_port(port)

    def test_invalid_hostname_fails_before_lookup(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                with mock.patch.object(script, "_lookup_target_addresses") as lookup:
                    for hostname in ("", "...", "portal.example;exit", "portal.example\n", None):
                        with self.assertRaises(script.VipResolutionError):
                            script.resolve_target_vips(hostname)
                lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
