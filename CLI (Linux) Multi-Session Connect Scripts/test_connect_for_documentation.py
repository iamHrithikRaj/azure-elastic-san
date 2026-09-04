import importlib.util
import json
import os
import threading
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "connect_for_documentation.py")
SPEC = importlib.util.spec_from_file_location("connect_for_documentation", SCRIPT_PATH)
connect = importlib.util.module_from_spec(SPEC)
with mock.patch("os.path.exists", side_effect=lambda path: path == "/usr/bin/apt-get"):
    SPEC.loader.exec_module(connect)


def completed_process(stdout="", stderr="", returncode=0):
    process = mock.Mock()
    process.communicate.return_value = (stdout.encode("utf-8"), stderr.encode("utf-8"))
    process.returncode = returncode
    return process


def locations_payload(region_name="eastus", mappings=None):
    if mappings is None:
        mappings = [{"logicalZone": "2", "physicalZone": "eastus-az3"}]
    return json.dumps(
        {
            "value": [
                {
                    "name": region_name,
                    "metadata": {"regionType": "Physical"},
                    "availabilityZoneMappings": mappings,
                }
            ]
        }
    )


def locations_list(region_name="eastus", mappings=None):
    return json.loads(locations_payload(region_name, mappings))["value"]


class ZonalAffinityTests(unittest.TestCase):
    def test_imds_query_bypasses_proxy_uses_metadata_header_and_timeout(self):
        response = mock.Mock()
        response.read.return_value = json.dumps(
            {
                "zone": "2",
                "subscriptionId": "00000000-0000-0000-0000-000000000001",
                "location": "eastus",
            }
        ).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        proxy_handler = object()

        with mock.patch.object(connect, "ProxyHandler", return_value=proxy_handler) as proxy:
            with mock.patch.object(connect, "build_opener", return_value=opener) as build:
                compute = connect.get_vm_compute_metadata()

        self.assertEqual("2", compute["zone"])
        proxy.assert_called_once_with({})
        build.assert_called_once_with(proxy_handler)
        request = opener.open.call_args.args[0]
        self.assertEqual("true", request.get_header("Metadata"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(
            connect.IMDS_TIMEOUT_SECONDS, opener.open.call_args.kwargs["timeout"]
        )
        response.close.assert_called_once_with()

    def test_success_resolves_current_subscription_and_normalizes_region(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        processes = [
            completed_process(subscription_id + "\n"),
            completed_process(" eastus \n"),
            completed_process(locations_payload(region_name="EaStUs")),
        ]
        compute = {
            "zone": "2",
            "subscriptionId": subscription_id.upper(),
            "location": "EASTUS",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", side_effect=processes) as popen:
                physical_zone = connect.resolve_physical_zone(None, "rg", "san")

        self.assertEqual("eastus-az3", physical_zone)
        self.assertEqual(
            ["az", "account", "show", "--query", "id", "--output", "tsv"],
            popen.call_args_list[0].args[0],
        )
        self.assertEqual(
            [
                "az",
                "elastic-san",
                "show",
                "-g",
                "rg",
                "-e",
                "san",
                "--subscription",
                subscription_id,
                "--query",
                "location",
                "--output",
                "tsv",
            ],
            popen.call_args_list[1].args[0],
        )
        self.assertEqual(
            [
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                (
                    "https://management.azure.com/subscriptions/"
                    "00000000-0000-0000-0000-000000000001/locations"
                    "?api-version=2022-12-01"
                ),
            ],
            popen.call_args_list[2].args[0],
        )

    def test_explicit_subscription_must_match_imds_subscription(self):
        cli_subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000002",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect.subprocess,
                "Popen",
                return_value=completed_process(cli_subscription_id),
            ) as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError,
                    "Elastic SAN subscription .* does not match VM subscription",
                ):
                    connect.resolve_physical_zone(
                        "production-subscription", "rg", "san"
                    )

        self.assertEqual(
            [
                "az",
                "account",
                "show",
                "--subscription",
                "production-subscription",
                "--query",
                "id",
                "--output",
                "tsv",
            ],
            popen.call_args.args[0],
        )
        self.assertEqual(1, popen.call_count)

    def test_non_zonal_vm_is_rejected(self):
        compute = {
            "zone": "",
            "subscriptionId": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "not availability-zone pinned"):
                connect.resolve_physical_zone(None, "rg", "san")

    def test_imds_failure_is_explicit(self):
        opener = mock.Mock()
        opener.open.side_effect = connect.URLError("unreachable")

        with mock.patch.object(connect, "build_opener", return_value=opener):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "IMDS request failed"):
                connect.get_vm_compute_metadata()

    def test_cli_failure_is_explicit(self):
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }
        process = completed_process(stderr="Please run 'az login'.", returncode=1)

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError, "Please run 'az login'"
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

    def test_invalid_cli_subscription_guid_is_rejected_before_url_construction(self):
        with mock.patch.object(
            connect.subprocess,
            "Popen",
            return_value=completed_process("../../other-resource"),
        ) as popen:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "must be a GUID"):
                connect.resolve_elastic_san_subscription_id(None)

        self.assertEqual(1, popen.call_count)

    def test_location_url_rejects_invalid_subscription_guid_before_cli_call(self):
        with mock.patch.object(connect, "_run_az_command") as run:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "must be a GUID"):
                connect.get_azure_locations("../../other-resource")

        run.assert_not_called()

    def test_invalid_imds_subscription_guid_is_rejected(self):
        compute = {
            "zone": "1",
            "subscriptionId": "not-a-guid",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError, "IMDS subscription ID must be a GUID"
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

        popen.assert_not_called()

    def test_azure_cli_timeout_kills_and_reaps_process(self):
        class HangingProcess(object):
            def __init__(self):
                self.release = threading.Event()
                self.killed = False
                self.reaped = False

            def communicate(self):
                self.release.wait()
                self.reaped = True
                return b"", b""

            def kill(self):
                self.killed = True
                self.release.set()

        process = HangingProcess()
        with mock.patch.object(connect.subprocess, "Popen", return_value=process):
            with mock.patch.object(connect, "AZ_CLI_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "timed out"):
                    connect._run_az_command(["az", "account", "show"], "Azure CLI test")

        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)

    def test_location_rest_response_requires_top_level_value_array(self):
        for payload in ("[]", "{}", '{"value": {}}'):
            with self.subTest(payload=payload):
                with mock.patch.object(connect, "_run_az_command", return_value=payload):
                    with self.assertRaisesRegex(
                        connect.ZonalAffinityError,
                        "JSON object|top-level 'value' array",
                    ):
                        connect.get_azure_locations(
                            "00000000-0000-0000-0000-000000000001"
                        )

    def test_elastic_san_location_lookup_uses_canonical_subscription(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        with mock.patch.object(
            connect, "_run_az_command", return_value=" eastus2 \n"
        ) as run:
            location = connect.get_elastic_san_location(
                subscription_id, "resource-group", "elastic-san"
            )

        self.assertEqual("eastus2", location)
        run.assert_called_once_with(
            [
                "az",
                "elastic-san",
                "show",
                "-g",
                "resource-group",
                "-e",
                "elastic-san",
                "--subscription",
                subscription_id,
                "--query",
                "location",
                "--output",
                "tsv",
            ],
            "Elastic SAN location lookup",
        )

    def test_different_vm_and_elastic_san_regions_are_rejected_before_mapping(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "2",
            "subscriptionId": subscription_id,
            "location": "eastus",
        }
        processes = [
            completed_process(subscription_id),
            completed_process("westus"),
        ]

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", side_effect=processes) as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError,
                    r"^The VM and Elastic SAN must be in the same region\.$",
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

        self.assertEqual(2, popen.call_count)

    def test_elastic_san_subscription_options_parse_identically(self):
        parser = connect.create_argument_parser()
        preferred = parser.parse_args(
            ["--elastic-san-subscription", "san-subscription"]
        )
        compatibility = parser.parse_args(["--subscription", "san-subscription"])

        self.assertEqual(
            "san-subscription", preferred.elastic_san_subscription
        )
        self.assertEqual(
            preferred.elastic_san_subscription,
            compatibility.elastic_san_subscription,
        )
        self.assertFalse(hasattr(preferred, "subscription"))

    def test_missing_region_is_rejected(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "no region matching"):
            connect.map_logical_to_physical_zone(
                locations_list(region_name="westus"), "eastus", "2"
            )

    def test_malformed_region_is_rejected(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "malformed region entry"):
            connect.map_logical_to_physical_zone([None], "eastus", "2")

    def test_missing_malformed_and_unmatched_zone_mappings_are_rejected(self):
        cases = [
            (
                [{"name": "eastus", "metadata": {"regionType": "Physical"}}],
                "missing availabilityZoneMappings",
            ),
            (
                [
                    {
                        "name": "eastus",
                        "metadata": {"regionType": "Physical"},
                        "availabilityZoneMappings": "invalid",
                    }
                ],
                "malformed availabilityZoneMappings",
            ),
            (
                json.loads(
                    locations_payload(
                        mappings=[{"logicalZone": "2", "physicalZone": ""}]
                    )
                )["value"],
                "malformed entry",
            ),
            (
                json.loads(
                    locations_payload(
                        mappings=[{"logicalZone": "1", "physicalZone": "eastus-az1"}]
                    )
                )["value"],
                "no availability-zone mapping",
            ),
        ]

        for locations, error_pattern in cases:
            with self.subTest(error_pattern=error_pattern):
                with self.assertRaisesRegex(connect.ZonalAffinityError, error_pattern):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_duplicate_trimmed_logical_zones_are_rejected(self):
        locations = locations_list(
            mappings=[
                {"logicalZone": " 2 ", "physicalZone": " eastus-az2 "},
                {"logicalZone": "2", "physicalZone": "eastus-az3"},
            ]
        )

        with self.assertRaisesRegex(
            connect.ZonalAffinityError, "duplicate logical zone '2'"
        ):
            connect.map_logical_to_physical_zone(locations, " eastus ", " 2 ")

    def test_mapping_trims_logical_and_physical_zones(self):
        locations = locations_list(
            mappings=[
                {"logicalZone": " 2 ", "physicalZone": " eastus-az3 "}
            ]
        )

        self.assertEqual(
            "eastus-az3",
            connect.map_logical_to_physical_zone(
                locations, " EASTUS ", " 2 "
            ),
        )

    def test_iqn_suffix_is_lowercase_and_rejects_unsafe_characters(self):
        self.assertEqual(
            "iqn.2024-01.com.microsoft:volume:az-eastus-az3",
            connect.decorate_target_iqn(
                "iqn.2024-01.com.microsoft:volume", "EASTUS-AZ3"
            ),
        )
        with self.assertRaisesRegex(connect.ZonalAffinityError, "unsafe"):
            connect.decorate_target_iqn(
                "iqn.2024-01.com.microsoft:volume", "eastus_az3"
            )

    def test_iqn_length_is_enforced_in_utf8_bytes(self):
        suffix = ":az-eastus-az3"
        allowed_iqn = "i" * (connect.MAX_IQN_UTF8_BYTES - len(suffix))
        self.assertEqual(
            connect.MAX_IQN_UTF8_BYTES,
            len(connect.decorate_target_iqn(allowed_iqn, "eastus-az3").encode("utf-8")),
        )
        with self.assertRaisesRegex(connect.ZonalAffinityError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn + "x", "eastus-az3")
        with self.assertRaisesRegex(connect.ZonalAffinityError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn[:-1] + "\u00e9", "eastus-az3")

    def test_opt_out_uses_original_iqn_without_zone_resolution(self):
        with mock.patch.object(
            connect, "resolve_physical_zone"
        ) as resolve_physical_zone:
            with mock.patch.object(
                connect,
                "get_iqns",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "check_connection", return_value=False) as check:
                    with mock.patch.object(connect, "connect_volume") as connect_volume:
                        connect.connect_volumes(
                            None, "rg", "san", "vg", ["volume1"], 4, False
                        )

        resolve_physical_zone.assert_not_called()
        check.assert_called_once_with("iqn.original", "portal.example", 3260)
        connect_volume.assert_called_once_with(
            "volume1", "iqn.original", "portal.example", 3260, 4
        )

    def test_opt_in_uses_decorated_iqn_for_connection_commands(self):
        with mock.patch.object(
            connect, "resolve_physical_zone", return_value="EASTUS-AZ3"
        ) as resolve_physical_zone:
            with mock.patch.object(
                connect,
                "get_iqns",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "check_connection", return_value=False) as check:
                    with mock.patch.object(connect, "connect_volume") as connect_volume:
                        connect.connect_volumes(
                            "sub", "rg", "san", "vg", ["volume1"], 4, True
                        )

        decorated_iqn = "iqn.original:az-eastus-az3"
        resolve_physical_zone.assert_called_once_with("sub", "rg", "san")
        check.assert_called_once_with(decorated_iqn, "portal.example", 3260)
        connect_volume.assert_called_once_with(
            "volume1", decorated_iqn, "portal.example", 3260, 4
        )


if __name__ == "__main__":
    unittest.main()
