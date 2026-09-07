import importlib.util
import json
import os
import tempfile
import threading
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "connect_for_documentation.py")
SPEC = importlib.util.spec_from_file_location("freebsd_connect_for_documentation", SCRIPT_PATH)
connect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect)

# The script targets FreeBSD/POSIX, where os.chown always exists. This test
# suite is also run on non-POSIX developer machines (e.g. Windows), which
# have no os.chown at all. Rather than weaken the production ownership-
# preservation behavior, shim in a no-op chown purely for the integration
# tests below that exercise the real filesystem end-to-end -- on FreeBSD,
# chowning a file back to its own current owner (the only thing these tests
# do) is always a no-op success anyway.
if not hasattr(os, "chown"):
    os.chown = lambda *_args, **_kwargs: None

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


def verbose_session_block(target_name, target_portal, state="Connected"):
    return (
        "Session ID:               1\n"
        "Initiator name:            iqn.1994-09.org.freebsd:host\n"
        "Initiator portal:          0.0.0.0\n"
        "Initiator alias:           \n"
        "Target name:               {target_name}\n"
        "Target portal:             {target_portal}\n"
        "Target alias:              \n"
        "User:                      \n"
        "Secret:                    \n"
        "Mutual user:               \n"
        "Mutual secret:             \n"
        "Session type:              Normal\n"
        "Enable:                    Yes\n"
        "Session state:             {state}\n"
        "Failure reason:            \n"
        "Header digest:             CRC32C\n"
        "Data digest:               CRC32C\n"
    ).format(target_name=target_name, target_portal=target_portal, state=state)


class ArgumentParsingTests(unittest.TestCase):
    def test_elastic_san_subscription_options_parse_identically(self):
        parser = connect.create_argument_parser()
        preferred = parser.parse_args(["--elastic-san-subscription", "san-subscription"])
        compatibility = parser.parse_args(["--subscription", "san-subscription"])

        self.assertEqual("san-subscription", preferred.elastic_san_subscription)
        self.assertEqual(
            preferred.elastic_san_subscription, compatibility.elastic_san_subscription
        )
        self.assertFalse(hasattr(preferred, "subscription"))

    def test_short_and_long_flags_parse_to_expected_dests(self):
        parser = connect.create_argument_parser()
        args = parser.parse_args(
            [
                "-g",
                "rg",
                "-e",
                "san",
                "-v",
                "vg",
                "-n",
                "vol1",
                "vol2",
                "-s",
                "1",
                "--enable-zonal-affinity",
                "--dry-run",
            ]
        )
        self.assertEqual("rg", args.resource_group)
        self.assertEqual("san", args.elastic_san)
        self.assertEqual("vg", args.volume_group)
        self.assertEqual(["vol1", "vol2"], args.volumes)
        self.assertEqual("1", args.num_of_sessions)
        self.assertTrue(args.enable_zonal_affinity)
        self.assertTrue(args.dry_run)

    def test_dry_run_and_zonal_affinity_default_to_false(self):
        parser = connect.create_argument_parser()
        args = parser.parse_args(["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1"])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.enable_zonal_affinity)
        self.assertEqual("1", args.num_of_sessions)

    def test_missing_required_arguments_raise_before_any_azure_or_freebsd_call(self):
        with mock.patch.object(connect, "check_az_cli_available") as check_az:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "Need to provide"):
                connect.main(["-g", "rg"])
        check_az.assert_not_called()

    def test_any_session_count_other_than_one_is_rejected_before_discovery(self):
        for value in ("0", "-1", "2", "32", "not-a-number"):
            with self.subTest(value=value):
                with mock.patch.object(connect, "check_az_cli_available") as check_az:
                    with self.assertRaisesRegex(
                        connect.ElasticSanConnectError,
                        "(must be exactly 1|rejects duplicate target-name/portal sessions)",
                    ):
                        connect.main(
                            [
                                "-g",
                                "rg",
                                "-e",
                                "san",
                                "-v",
                                "vg",
                                "-n",
                                "vol1",
                                "-s",
                                value,
                            ]
                        )
                check_az.assert_not_called()


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

    def test_explicit_subscription_must_match_imds_subscription(self):
        cli_subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000002",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect.subprocess, "Popen", return_value=completed_process(cli_subscription_id)
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError,
                    "Elastic SAN subscription .* does not match VM subscription",
                ):
                    connect.resolve_physical_zone("production-subscription", "rg", "san")

    def test_non_zonal_vm_is_rejected(self):
        compute = {"zone": "", "subscriptionId": "00000000-0000-0000-0000-000000000001", "location": "eastus"}
        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "not availability-zone pinned"):
                connect.resolve_physical_zone(None, "rg", "san")

    def test_different_vm_and_elastic_san_regions_are_rejected(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {"zone": "2", "subscriptionId": subscription_id, "location": "eastus"}
        processes = [completed_process(subscription_id), completed_process("westus")]
        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", side_effect=processes):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError,
                    r"^The VM and Elastic SAN must be in the same region\.$",
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

    def test_missing_malformed_and_unmatched_zone_mappings_are_rejected(self):
        cases = [
            (
                [{"name": "eastus", "metadata": {"regionType": "Physical"}}],
                "missing availabilityZoneMappings",
            ),
            (
                json.loads(locations_payload(mappings=[{"logicalZone": "2", "physicalZone": ""}]))[
                    "value"
                ],
                "malformed entry",
            ),
            (
                json.loads(
                    locations_payload(mappings=[{"logicalZone": "1", "physicalZone": "eastus-az1"}])
                )["value"],
                "no availability-zone mapping",
            ),
        ]
        for locations, error_pattern in cases:
            with self.subTest(error_pattern=error_pattern):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, error_pattern):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_duplicate_trimmed_logical_zones_are_rejected(self):
        locations = json.loads(
            locations_payload(
                mappings=[
                    {"logicalZone": " 2 ", "physicalZone": " eastus-az2 "},
                    {"logicalZone": "2", "physicalZone": "eastus-az3"},
                ]
            )
        )["value"]
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "duplicate logical zone '2'"):
            connect.map_logical_to_physical_zone(locations, " eastus ", " 2 ")

    def test_mapping_trims_logical_and_physical_zones(self):
        locations = json.loads(
            locations_payload(mappings=[{"logicalZone": " 2 ", "physicalZone": " eastus-az3 "}])
        )["value"]
        self.assertEqual(
            "eastus-az3", connect.map_logical_to_physical_zone(locations, " EASTUS ", " 2 ")
        )

    def test_iqn_suffix_is_lowercase_and_rejects_unsafe_characters(self):
        self.assertEqual(
            "iqn.2024-01.com.microsoft:volume:az-eastus-az3",
            connect.decorate_target_iqn("iqn.2024-01.com.microsoft:volume", "EASTUS-AZ3"),
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "unsafe"):
            connect.decorate_target_iqn("iqn.2024-01.com.microsoft:volume", "eastus_az3")

    def test_iqn_length_is_enforced_in_utf8_bytes(self):
        suffix = ":az-eastus-az3"
        allowed_iqn = "i" * (connect.MAX_IQN_UTF8_BYTES - len(suffix))
        self.assertEqual(
            connect.MAX_IQN_UTF8_BYTES,
            len(connect.decorate_target_iqn(allowed_iqn, "eastus-az3").encode("utf-8")),
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn + "x", "eastus-az3")

    def test_azure_cli_timeout_kills_and_reaps_process(self):
        class HangingProcess(object):
            def __init__(self):
                self.release = threading.Event()
                self.killed = False

            def communicate(self):
                self.release.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.release.set()

        process = HangingProcess()
        with mock.patch.object(connect.subprocess, "Popen", return_value=process):
            with mock.patch.object(connect, "AZ_CLI_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "timed out"):
                    connect._run_az_command(["az", "account", "show"], "Azure CLI test")
        self.assertTrue(process.killed)


class AzCliAvailabilityTests(unittest.TestCase):
    def test_missing_az_binary_raises_with_freebsd_specific_guidance(self):
        with mock.patch.object(connect.shutil, "which", return_value=None):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "FreeBSD Ports"):
                connect.check_az_cli_available()

    def test_missing_elastic_san_extension_raises(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/local/bin/az"):
            with mock.patch.object(
                connect, "_run_az_command", return_value="storage-preview\nresource-graph\n"
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "elastic-san' extension is not installed"
                ):
                    connect.check_az_cli_available()

    def test_present_extension_passes_without_raising(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/local/bin/az"):
            with mock.patch.object(
                connect, "_run_az_command", return_value="elastic-san\n"
            ):
                connect.check_az_cli_available()  # should not raise


class FreeBsdMutationPreflightTests(unittest.TestCase):
    def test_requires_root(self):
        with mock.patch.object(connect.os, "geteuid", return_value=1000, create=True):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "must be run as root"):
                connect.check_freebsd_mutation_prerequisites()

    def test_requires_freebsd_tools_on_path(self):
        with mock.patch.object(connect.os, "geteuid", return_value=0, create=True):
            with mock.patch.object(connect.shutil, "which", return_value=None):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "iscsictl, service, sysrc"):
                    connect.check_freebsd_mutation_prerequisites()

    def test_requires_iscsi_device_node(self):
        with mock.patch.object(connect.os, "geteuid", return_value=0, create=True):
            with mock.patch.object(connect.shutil, "which", return_value="/usr/sbin/tool"):
                with mock.patch.object(connect.os.path, "exists", return_value=False):
                    with self.assertRaisesRegex(connect.ElasticSanConnectError, "/dev/iscsi"):
                        connect.check_freebsd_mutation_prerequisites()

    def test_passes_when_root_tools_and_device_are_present(self):
        with mock.patch.object(connect.os, "geteuid", return_value=0, create=True):
            with mock.patch.object(connect.shutil, "which", return_value="/usr/sbin/tool"):
                with mock.patch.object(connect.os.path, "exists", return_value=True):
                    connect.check_freebsd_mutation_prerequisites()  # should not raise


class ConfigValueValidationTests(unittest.TestCase):
    def test_rejects_quotes_braces_hash_semicolon(self):
        for bad_value in ['bad"value', "bad{value", "bad}value", "bad#value", "bad;value"]:
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "not permitted"):
                    connect.validate_config_scalar(bad_value, "field")

    def test_rejects_embedded_newline_and_control_characters(self):
        for bad_value in ["line1\nline2", "tab\tvalue", "cr\rvalue"]:
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "control character"):
                    connect.validate_config_scalar(bad_value, "field")

    def test_rejects_empty_value(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "non-empty"):
            connect.validate_config_scalar("", "field")

    def test_accepts_typical_iqn_and_address(self):
        self.assertEqual(
            "iqn.2005-03.com.microsoft:san.volume",
            connect.validate_config_scalar("iqn.2005-03.com.microsoft:san.volume", "field"),
        )
        self.assertEqual(
            "10.0.0.4:3260", connect.validate_config_scalar("10.0.0.4:3260", "field")
        )


class NicknameTests(unittest.TestCase):
    def test_deterministic_across_calls(self):
        first = connect.build_nickname("vg-1", "vol-1", 1)
        second = connect.build_nickname("vg-1", "vol-1", 1)
        self.assertEqual(first, second)

    def test_rejects_session_index_other_than_one(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "exactly one session"):
            connect.build_nickname("vg-1", "vol-1", 2)

    def test_distinct_for_different_volumes(self):
        self.assertNotEqual(
            connect.build_nickname("vg-1", "vol-1", 1),
            connect.build_nickname("vg-1", "vol-2", 1),
        )

    def test_sanitizes_unsafe_characters_and_lowercases(self):
        nickname = connect.build_nickname("VG_1", "Volume One!", 1)
        self.assertRegex(nickname, r"^[a-z0-9][a-z0-9_-]*$")
        self.assertTrue(nickname.startswith("esan-vg-1-volume-one-s1"))

    def test_raises_when_no_usable_characters_remain(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "no \\[a-z0-9\\] characters"):
            connect.build_nickname("!!!", "vol", 1)

    def test_maximum_resource_names_produce_bounded_ascii_nickname(self):
        nickname = connect.build_nickname("g" * 63, "v" * 63, 1)
        self.assertLessEqual(len(nickname), 128)
        self.assertRegex(nickname, r"^esan-[a-z0-9-]+-[0-9a-f]{12}-s1$")
        self.assertEqual(nickname, connect.build_nickname("g" * 63, "v" * 63, 1))

    def test_long_identities_with_same_readable_prefix_do_not_collide(self):
        first = connect.build_nickname("g" * 63, "v" * 62 + "a", 1)
        second = connect.build_nickname("g" * 63, "v" * 62 + "b", 1)
        self.assertNotEqual(first, second)
        self.assertEqual(first[:100], second[:100])


class ManagedBlockRenderTests(unittest.TestCase):
    def test_render_contains_required_fields_and_markers(self):
        entries = [
            connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260")
        ]
        block = connect.render_managed_block(entries).decode("utf-8")
        self.assertIn(connect.MANAGED_BLOCK_BEGIN, block)
        self.assertIn(connect.MANAGED_BLOCK_END, block)
        self.assertIn("esan-vg-vol-s1 {", block)
        self.assertIn('TargetName    = "iqn.example:target"', block)
        self.assertIn('TargetAddress = "10.0.0.1:3260"', block)
        self.assertIn("HeaderDigest  = CRC32C", block)
        self.assertIn("DataDigest    = CRC32C", block)
        self.assertIn("Enable        = On", block)


class ConfigSplicingTests(unittest.TestCase):
    def test_appends_block_when_no_markers_present(self):
        existing = b"# unrelated stanza\nfoo { targetname = bar; }\n"
        new_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-new-s1", "iqn.example:new", "new:3260")]
        )
        result = connect.compute_new_config_content(existing, new_block)
        self.assertTrue(result.startswith(existing))
        self.assertTrue(result.endswith(new_block))

    def test_replaces_selected_entry_and_preserves_surrounding_content_byte_for_byte(self):
        old_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:old", "old:3260")]
        )
        existing = (
            b"# my manual stanza\n"
            b"manual { targetname = manual.target; }\n"
            + old_block
            + b"# trailing manual comment\n"
        )
        new_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:new", "new:3260")]
        )
        result = connect.compute_new_config_content(existing, new_block)

        self.assertIn(b"# my manual stanza\nmanual { targetname = manual.target; }\n", result)
        self.assertIn(b"# trailing manual comment\n", result)
        self.assertIn(b"iqn.example:new", result)
        self.assertNotIn(b"iqn.example:old", result)
        self.assertEqual(
            b"# trailing manual comment\n",
            result[result.index(b"# trailing manual comment\n") :],
        )

    def test_corrupted_single_marker_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        existing = b"prefix\n" + begin + b"\nstray content\n"
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "only one of"):
            connect.compute_new_config_content(existing, b"anything")

    def test_end_before_begin_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        end = connect.MANAGED_BLOCK_END.encode("utf-8")
        existing = end + b"\nstuff\n" + begin + b"\n"
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "before its BEGIN marker"):
            connect.compute_new_config_content(existing, b"anything")

    def test_duplicate_marker_pair_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        end = connect.MANAGED_BLOCK_END.encode("utf-8")
        existing = (
            begin + b"\nfirst-nickname { }\n" + end + b"\n"
            b"# accidental duplicate below\n"
            + begin + b"\nsecond-nickname { }\n" + end + b"\n"
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "more than one managed-block"):
            connect.compute_new_config_content(existing, b"anything")

    def test_idempotent_rerender_of_same_plan_is_byte_identical(self):
        entries = [
            connect.ManagedConfigEntry(
                "esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260"
            )
        ]
        first = connect.render_managed_block(entries)
        second = connect.render_managed_block(entries)
        self.assertEqual(first, second)

    def test_selecting_b_preserves_a_and_selecting_a_preserves_b(self):
        entry_a = connect.ManagedConfigEntry(
            "esan-vg-a-s1", "iqn.example:a", "a.example:3260"
        )
        entry_b = connect.ManagedConfigEntry(
            "esan-vg-b-s1", "iqn.example:b", "b.example:3260"
        )
        original = b"# exact prefix\xff\n" + connect.render_managed_block([entry_a]) + b"\x00tail"

        with_b = connect.compute_new_config_content(
            original, connect.render_managed_block([entry_b])
        )
        with_a_again = connect.compute_new_config_content(
            with_b, connect.render_managed_block([entry_a])
        )

        self.assertIn(b"esan-vg-a-s1", with_b)
        self.assertIn(b"esan-vg-b-s1", with_b)
        self.assertEqual(with_b, with_a_again)
        self.assertTrue(with_b.startswith(b"# exact prefix\xff\n"))
        self.assertTrue(with_b.endswith(b"\x00tail"))

        a_then_b = connect.compute_new_config_content(
            connect.compute_new_config_content(
                b"", connect.render_managed_block([entry_a])
            ),
            connect.render_managed_block([entry_b]),
        )
        b_then_a = connect.compute_new_config_content(
            connect.compute_new_config_content(
                b"", connect.render_managed_block([entry_b])
            ),
            connect.render_managed_block([entry_a]),
        )
        self.assertEqual(a_then_b, b_then_a)

    def test_malformed_or_unknown_managed_content_is_rejected(self):
        malformed = (
            connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
            + b"\n# unknown content\n"
            + connect.MANAGED_BLOCK_END.encode("utf-8")
            + b"\n"
        )
        selected = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "p:3260")]
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "unknown content"):
            connect.compute_new_config_content(malformed, selected)

    def test_empty_selected_block_is_rejected(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "empty"):
            connect.compute_new_config_content(b"", connect.render_managed_block([]))


class PortalMarkerCompatibilityTests(unittest.TestCase):
    """Both this standalone script and the Azure portal's generated FreeBSD connect script can
    write to the same /etc/iscsi.conf. They must use byte-for-byte identical managed-block
    markers so that whichever one ran most recently recognizes and safely merges the other's
    managed entries, rather than treating them as a stray/corrupted marker (see
    "MANAGED_BLOCK_BEGIN"/"MANAGED_BLOCK_END" in this file and generateFreeBsdConnectScript in
    VolumeHelpers.ts). These constants are duplicated here (rather than imported, since the portal
    generator is TypeScript) and must be kept in sync by hand with VolumeHelpers.ts."""

    PORTAL_MANAGED_BLOCK_BEGIN = "# BEGIN AZURE ELASTIC SAN FREEBSD MANAGED BLOCK -- DO NOT EDIT"
    PORTAL_MANAGED_BLOCK_END = "# END AZURE ELASTIC SAN FREEBSD MANAGED BLOCK"

    def test_marker_pair_matches_portal_generated_script(self):
        self.assertEqual(connect.MANAGED_BLOCK_BEGIN, self.PORTAL_MANAGED_BLOCK_BEGIN)
        self.assertEqual(connect.MANAGED_BLOCK_END, self.PORTAL_MANAGED_BLOCK_END)

    def test_content_rendered_with_portal_markers_is_recognized_and_merged(self):
        # Simulate the exact byte content the Azure portal's generated FreeBSD connect script
        # would have written to /etc/iscsi.conf, then confirm this script's splice logic treats
        # it as an ordinary existing managed block (not corruption) and merges into it.
        portal_written_content = (
            connect.render_managed_block(
                [
                    connect.ManagedConfigEntry(
                        "esan-portal-vol-s1", "iqn.portal.example", "portal.example:3260"
                    )
                ]
            )
        )
        entries = [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260")]
        new_block = connect.render_managed_block(entries)

        result = connect.compute_new_config_content(portal_written_content, new_block)

        self.assertIn(b"esan-portal-vol-s1", result)
        self.assertIn(b"esan-vg-vol-s1", result)


class AtomicWriteAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")

    def tearDown(self):
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def test_write_is_atomic_and_uses_os_replace_in_same_directory(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original content\n")
        original_stat = os.stat(self.config_path)

        with mock.patch.object(connect.os, "chown", create=True):
            with mock.patch.object(connect.os, "replace", wraps=os.replace) as replace_mock:
                connect.write_config_atomically(self.config_path, b"new content\n", original_stat)
        replace_mock.assert_called_once()
        temp_arg, final_arg = replace_mock.call_args.args
        self.assertEqual(os.path.dirname(temp_arg), self.tempdir)
        self.assertEqual(final_arg, self.config_path)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"new content\n", handle.read())

    def test_write_preserves_permissions_and_ownership_of_existing_file(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original\n")

        class FakeStat(object):
            st_mode = 0o100640
            st_uid = 4242
            st_gid = 4343

        with mock.patch.object(connect.os, "chmod") as chmod_mock:
            with mock.patch.object(connect.os, "chown", create=True) as chown_mock:
                connect.write_config_atomically(self.config_path, b"updated\n", FakeStat())

        chmod_mock.assert_called_once()
        temp_path_arg, mode_arg = chmod_mock.call_args.args
        self.assertEqual(0o640, mode_arg)
        chown_mock.assert_called_once_with(temp_path_arg, 4242, 4343)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"updated\n", handle.read())

    def test_new_file_gets_default_permissions_when_no_prior_stat(self):
        with mock.patch.object(connect.os, "chmod") as chmod_mock:
            connect.write_config_atomically(self.config_path, b"brand new\n", None)
        chmod_mock.assert_called_once()
        _, mode_arg = chmod_mock.call_args.args
        self.assertEqual(0o644, mode_arg)
        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"brand new\n", handle.read())

    def test_chown_failure_is_not_swallowed(self):
        with mock.patch.object(connect.os, "chmod"):
            with mock.patch.object(
                connect.os, "chown", create=True, side_effect=OSError("not permitted")
            ):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "preserve ownership"):
                    connect.write_config_atomically(
                        self.config_path, b"data\n", os.stat_result((0o100640, 0, 0, 0, 0, 0, 0, 0, 0, 0))
                    )
        # The temp file created for the failed write must not leak, and the
        # final destination must not have been created/touched.
        self.assertEqual([], os.listdir(self.tempdir))
        self.assertFalse(os.path.exists(self.config_path))

    def test_backup_temp_file_does_not_leak_on_failure(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"pre-existing\n")
        content, existed, _ = connect.read_config_file(self.config_path)

        with mock.patch.object(connect.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                connect.backup_config(self.config_path, content, existed)

        remaining = os.listdir(self.tempdir)
        self.assertEqual(["iscsi.conf"], remaining)

    def test_backup_created_only_when_file_previously_existed(self):
        content, existed, _ = connect.read_config_file(self.config_path)
        self.assertFalse(existed)
        self.assertIsNone(connect.backup_config(self.config_path, content, existed))

        with open(self.config_path, "wb") as handle:
            handle.write(b"pre-existing\n")
        content, existed, _ = connect.read_config_file(self.config_path)
        self.assertTrue(existed)
        backup_path = connect.backup_config(self.config_path, content, existed)
        self.assertIsNotNone(backup_path)
        with open(backup_path, "rb") as handle:
            self.assertEqual(b"pre-existing\n", handle.read())

    def test_rollback_restores_original_content_when_file_existed(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original\n")
        content, existed, original_stat = connect.read_config_file(self.config_path)
        backup_path = connect.backup_config(self.config_path, content, existed)

        connect.write_config_atomically(self.config_path, b"mutated\n", original_stat)
        connect.restore_config_from_backup(self.config_path, backup_path, existed, original_stat)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"original\n", handle.read())

    def test_rollback_removes_file_that_did_not_exist_before(self):
        connect.write_config_atomically(self.config_path, b"brand new\n", None)
        self.assertTrue(os.path.exists(self.config_path))
        connect.restore_config_from_backup(self.config_path, None, False, None)
        self.assertFalse(os.path.exists(self.config_path))


class SessionParsingTests(unittest.TestCase):
    def test_parses_target_name_and_portal_from_verbose_output(self):
        output = verbose_session_block("iqn.example:target", "10.0.0.4:3260")
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(1, len(sessions))
        self.assertEqual("iqn.example:target", sessions[0]["Target name"])
        self.assertEqual("10.0.0.4:3260", sessions[0]["Target portal"])
        self.assertEqual("Connected", sessions[0]["Session state"])

    def test_parses_multiple_sessions_separated_by_blank_line(self):
        output = "\n\n".join(
            [
                verbose_session_block("iqn.example:target", "10.0.0.4:3260"),
                verbose_session_block("iqn.example:target", "10.0.0.4:3260"),
                verbose_session_block("iqn.example:other", "10.0.0.5:3260"),
            ]
        )
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(3, len(sessions))
        self.assertEqual(2, len(connect.sessions_for_target(sessions, "iqn.example:target")))
        self.assertEqual(1, len(connect.sessions_for_target(sessions, "iqn.example:other")))
        self.assertEqual(0, len(connect.sessions_for_target(sessions, "iqn.example:missing")))

    def test_parses_adjacent_sessions_without_blank_separator(self):
        output = (
            verbose_session_block("iqn.example:first", "first:3260").rstrip()
            + "\n"
            + verbose_session_block("iqn.example:second", "second:3260")
        )
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(
            ["iqn.example:first", "iqn.example:second"],
            [session["Target name"] for session in sessions],
        )

    def test_empty_output_means_zero_sessions(self):
        self.assertEqual([], connect._parse_iscsictl_verbose_sessions(""))
        self.assertEqual([], connect._parse_iscsictl_verbose_sessions("\n\n"))

    def test_missing_required_session_field_is_rejected(self):
        malformed = verbose_session_block(
            "iqn.example:target", "target.example:3260"
        ).replace("Session state:             Connected\n", "")
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "missing Session state"):
            connect._parse_iscsictl_verbose_sessions(malformed)


class AddSessionsForVolumeTests(unittest.TestCase):
    def _plan(self):
        return connect.VolumeConnectionPlan(
            "vol1",
            "iqn.example:target",
            "10.0.0.4:3260",
            [connect.build_nickname("vg", "vol1", 1)],
        )

    def test_skips_connected_session_even_when_target_address_was_redirected(self):
        plan = self._plan()
        sessions = [
            {
                "Target name": plan.target_name,
                "Target portal": "redirected.example:3260",
                "Session state": "Connected",
                "Enable": "Yes",
            }
        ]
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=sessions):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)
        add_mock.assert_not_called()

    def test_submits_once_then_polls_only_requested_target_until_connected(self):
        plan = self._plan()
        unrelated = {
            "Target name": "iqn.example:unrelated",
            "Target portal": "other:3260",
            "Session state": "Disconnected",
            "Enable": "No",
        }
        connecting = {
            "Target name": plan.target_name,
            "Target portal": "redirected.example:3260",
            "Session state": "Connecting",
            "Enable": "Yes",
        }
        connected = dict(connecting, **{"Session state": "Connected"})
        clock = mock.Mock(side_effect=[0, 0, 1])
        with mock.patch.object(
            connect, "list_iscsi_sessions", side_effect=[[], [unrelated], [unrelated, connecting], [unrelated, connected]]
        ):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                with mock.patch.object(connect.time, "monotonic", clock):
                    with mock.patch.object(connect.time, "sleep"):
                        connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)
        add_mock.assert_called_once_with(plan.nicknames[0], "/etc/iscsi.conf")

    def test_disconnected_session_fails_without_add_or_remove(self):
        plan = self._plan()
        sessions = [
            {
                "Target name": plan.target_name,
                "Target portal": plan.target_address,
                "Session state": "Disconnected",
                "Enable": "Yes",
            }
        ]
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=sessions):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "recover or remove that specific session"
                ):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)
        add_mock.assert_not_called()

    def test_disabled_session_fails_without_adding_duplicate(self):
        plan = self._plan()
        sessions = [
            {
                "Target name": plan.target_name,
                "Target portal": plan.target_address,
                "Session state": "Connected",
                "Enable": "No",
            }
        ]
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=sessions):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "disabled"):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)
        add_mock.assert_not_called()

    def test_multiple_matching_sessions_fail_without_adding(self):
        plan = self._plan()
        sessions = [
            {
                "Target name": plan.target_name,
                "Session state": "Connected",
                "Enable": "Yes",
            },
            {
                "Target name": plan.target_name,
                "Session state": "Connected",
                "Enable": "Yes",
            },
        ]
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=sessions):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "exactly one"):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)
        add_mock.assert_not_called()

    def test_submission_error_is_preserved(self):
        plan = self._plan()
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
            with mock.patch.object(
                connect,
                "add_iscsi_session",
                side_effect=connect.ElasticSanConnectError("kernel rejected request"),
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "^kernel rejected request$"
                ):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf", 1)


class SessionReadinessPollingTests(unittest.TestCase):
    def test_timeout_is_bounded_and_reports_possible_live_session(self):
        times = iter([100.0, 100.0, 102.0, 105.0])
        sleeps = []
        with mock.patch.object(
            connect,
            "list_iscsi_sessions",
            return_value=[
                {
                    "Target name": "iqn.example:other",
                    "Session state": "Disconnected",
                    "Enable": "No",
                }
            ],
        ):
            with self.assertRaisesRegex(
                connect.ElasticSanConnectError, "may still create or leave a live session"
            ):
                connect.wait_for_connected_session(
                    "iqn.example:target",
                    timeout=5,
                    poll_interval=2,
                    monotonic=lambda: next(times),
                    sleep=sleeps.append,
                )
        self.assertEqual([2, 2], sleeps)


class ExactCommandArgvTests(unittest.TestCase):
    def test_ensure_iscsid_enabled_and_running_sets_unset_value_and_starts_service(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (1, "", "unknown variable"),
                    (1, "", "iscsid is not running"),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "iscsid_enable=YES"],
                    "sysrc iscsid_enable=YES",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "start"],
                    "service iscsid start",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_command.call_args_list,
        )

    def test_ensure_iscsid_enabled_and_running_preserves_yes_and_running_service(self):
        with mock.patch.object(connect, "_run_command") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (0, "YES\n", ""),
                    (0, "iscsid is running as pid 123.\n", ""),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        run_command.assert_not_called()

    def test_ensure_iscsid_enabled_and_running_changes_no_without_starting_running_service(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (0, "NO\n", ""),
                    (0, "iscsid is running as pid 123.\n", ""),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        run_command.assert_called_once_with(
            ["sysrc", "iscsid_enable=YES"],
            "sysrc iscsid_enable=YES",
            connect.SHORT_COMMAND_TIMEOUT_SECONDS,
        )

    def test_add_iscsi_session_argv(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            connect.add_iscsi_session("esan-vg-vol-s1", "/etc/iscsi.conf")
        run_command.assert_called_once_with(
            ["iscsictl", "-A", "-n", "esan-vg-vol-s1", "-c", "/etc/iscsi.conf"],
            "iscsictl add session 'esan-vg-vol-s1'",
            connect.ISCSICTL_COMMAND_TIMEOUT_SECONDS,
        )

    def test_list_iscsi_sessions_argv(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            connect.list_iscsi_sessions()
        run_command.assert_called_once_with(
            ["iscsictl", "-L", "-v"], "iscsictl session listing", connect.AZ_CLI_TIMEOUT_SECONDS
        )

    def test_never_calls_iscsictl_remove_all(self):
        with open(SCRIPT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        # Look for the actual argv-construction pattern, not the prose
        # description of the constraint that legitimately appears in the
        # module docstring.
        self.assertNotIn('"iscsictl", "-R"', source)
        self.assertNotIn("'iscsictl', '-R'", source)


class GetVolumeStorageTargetTests(unittest.TestCase):
    def test_argv_and_parsing(self):
        payload = json.dumps(
            {"targetIqn": "iqn.example:target", "targetPortalHostname": "10.0.0.4", "targetPortalPort": 3260}
        )
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run_az:
            iqn, hostname, port = connect.get_volume_storage_target(
                "sub", "rg", "san", "vg", "vol1"
            )
        self.assertEqual(("iqn.example:target", "10.0.0.4", 3260), (iqn, hostname, port))
        run_az.assert_called_once_with(
            [
                "az",
                "elastic-san",
                "volume",
                "show",
                "-g",
                "rg",
                "-e",
                "san",
                "-v",
                "vg",
                "-n",
                "vol1",
                "--query",
                "storageTarget",
                "--output",
                "json",
                "--subscription",
                "sub",
            ],
            "Elastic SAN volume 'vol1' storage target lookup",
        )

    def test_omits_subscription_flag_when_not_provided(self):
        payload = json.dumps(
            {"targetIqn": "iqn.example:target", "targetPortalHostname": "10.0.0.4", "targetPortalPort": 3260}
        )
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run_az:
            connect.get_volume_storage_target(None, "rg", "san", "vg", "vol1")
        command = run_az.call_args.args[0]
        self.assertNotIn("--subscription", command)

    def test_missing_field_raises(self):
        payload = json.dumps({"targetIqn": "iqn.example:target"})
        with mock.patch.object(connect, "_run_az_command", return_value=payload):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "missing"):
                connect.get_volume_storage_target(None, "rg", "san", "vg", "vol1")


class BuildConnectionPlanTests(unittest.TestCase):
    def test_opt_out_uses_original_iqn_without_zone_resolution(self):
        with mock.patch.object(connect, "resolve_physical_zone") as resolve_zone:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                plans = connect.build_connection_plan(
                    None, "rg", "san", "vg", ["volume1"], 1, enable_zonal_affinity=False
                )
        resolve_zone.assert_not_called()
        self.assertEqual("iqn.original", plans[0].target_name)
        self.assertEqual("portal.example:3260", plans[0].target_address)
        self.assertEqual(1, len(plans[0].nicknames))

    def test_opt_in_decorates_iqn_for_all_volumes(self):
        with mock.patch.object(connect, "resolve_physical_zone", return_value="EASTUS-AZ3") as resolve_zone:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                plans = connect.build_connection_plan(
                    "sub", "rg", "san", "vg", ["volume1", "volume2"], 1, enable_zonal_affinity=True
                )
        resolve_zone.assert_called_once_with("sub", "rg", "san")
        for plan in plans:
            self.assertEqual("iqn.original:az-eastus-az3", plan.target_name)

    def test_empty_volume_selection_is_rejected_before_discovery(self):
        with mock.patch.object(connect, "resolve_physical_zone") as resolve_zone:
            with mock.patch.object(connect, "get_volume_storage_target") as get_target:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "At least one volume"):
                    connect.build_connection_plan(
                        None, "rg", "san", "vg", [], 1, enable_zonal_affinity=True
                    )
        resolve_zone.assert_not_called()
        get_target.assert_not_called()


class ExecuteConnectionPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-exec-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")

    def tearDown(self):
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def _plans(self):
        return [
            connect.VolumeConnectionPlan(
                "vol1", "iqn.example:vol1", "10.0.0.4:3260", ["esan-vg-vol1-s1"]
            )
        ]

    def test_successful_run_writes_config_and_adds_sessions(self):
        plans = self._plans()
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"):
            with mock.patch.object(connect, "ensure_iscsid_enabled_and_running"):
                with mock.patch.object(connect, "list_iscsi_sessions", side_effect=[[], [
                    {
                        "Target name": "iqn.example:vol1",
                        "Target portal": "redirected:3260",
                        "Session state": "Connected",
                        "Enable": "Yes",
                    },
                ]]):
                    with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                        connect.execute_connection_plan(plans, self.config_path, 1)
        self.assertEqual(1, add_mock.call_count)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("esan-vg-vol1-s1", content)

    def test_failure_during_session_add_rolls_back_config_and_preserves_unrelated_content(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"# unrelated manual stanza\nmanual { targetname = manual; }\n")
        with open(self.config_path, "rb") as handle:
            original_content = handle.read()

        plans = self._plans()
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"):
            with mock.patch.object(connect, "ensure_iscsid_enabled_and_running"):
                with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
                    with mock.patch.object(
                        connect, "add_iscsi_session", side_effect=connect.ElasticSanConnectError("boom")
                    ):
                        with self.assertRaisesRegex(connect.ElasticSanConnectError, "restored"):
                            connect.execute_connection_plan(plans, self.config_path, 1)

        with open(self.config_path, "rb") as handle:
            restored_content = handle.read()
        self.assertEqual(original_content, restored_content)
        self.assertNotIn(b"esan-vg-vol1-s1", restored_content)

    def test_failure_when_config_did_not_exist_before_removes_it_on_rollback(self):
        self.assertFalse(os.path.exists(self.config_path))
        plans = self._plans()
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"):
            with mock.patch.object(connect, "ensure_iscsid_enabled_and_running"):
                with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
                    with mock.patch.object(
                        connect, "add_iscsi_session", side_effect=connect.ElasticSanConnectError("boom")
                    ):
                        with self.assertRaises(connect.ElasticSanConnectError):
                            connect.execute_connection_plan(plans, self.config_path, 1)
        self.assertFalse(os.path.exists(self.config_path))

    def test_rerun_with_all_sessions_already_connected_does_not_call_add(self):
        plans = self._plans()
        already_connected = [
            {
                "Target name": "iqn.example:vol1",
                "Target portal": "redirected:3260",
                "Session state": "Connected",
                "Enable": "Yes",
            },
        ]
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"):
            with mock.patch.object(connect, "ensure_iscsid_enabled_and_running"):
                with mock.patch.object(connect, "list_iscsi_sessions", return_value=already_connected):
                    with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                        connect.execute_connection_plan(plans, self.config_path, 1)
        add_mock.assert_not_called()

    def test_empty_plan_is_rejected_before_any_mutation(self):
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "empty"):
                connect.execute_connection_plan([], self.config_path, 1)
        preflight.assert_not_called()

    def test_multi_session_plan_is_rejected_before_any_mutation(self):
        plans = self._plans()
        plans[0].nicknames.append("esan-vg-vol1-s2")
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "exactly one"):
                connect.execute_connection_plan(plans, self.config_path, 2)
        preflight.assert_not_called()

    def test_malformed_existing_block_is_rejected_before_service_mutation(self):
        with open(self.config_path, "wb") as handle:
            handle.write(
                connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
                + b"\nunknown\n"
                + connect.MANAGED_BLOCK_END.encode("utf-8")
                + b"\n"
            )
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"):
            with mock.patch.object(connect, "ensure_iscsid_enabled_and_running") as ensure:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "unknown"):
                    connect.execute_connection_plan(self._plans(), self.config_path, 1)
        ensure.assert_not_called()


class DryRunTests(unittest.TestCase):
    def test_dry_run_performs_no_mutation_and_calls_no_freebsd_tooling(self):
        argv = [
            "-g",
            "rg",
            "-e",
            "san",
            "-v",
            "vg",
            "-n",
            "volume1",
            "-s",
            "1",
            "--dry-run",
        ]
        with mock.patch.object(connect, "check_az_cli_available") as check_az:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
                    with mock.patch.object(connect, "ensure_iscsid_enabled_and_running") as ensure_iscsid:
                        with mock.patch.object(connect, "_run_command") as run_command:
                            with mock.patch.object(connect, "_run_subprocess") as run_subprocess:
                                with mock.patch.object(connect.os, "replace") as os_replace:
                                    with mock.patch("builtins.open", mock.mock_open()) as open_mock:
                                        connect.main(argv)

        check_az.assert_called_once()
        preflight.assert_not_called()
        ensure_iscsid.assert_not_called()
        run_command.assert_not_called()
        run_subprocess.assert_not_called()
        os_replace.assert_not_called()
        open_mock.assert_not_called()

    def test_dry_run_does_not_require_root(self):
        argv = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "volume1", "--dry-run"]
        with mock.patch.object(connect, "check_az_cli_available"):
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect.os, "geteuid", create=True) as geteuid:
                    connect.main(argv)
        geteuid.assert_not_called()

    def test_dry_run_with_zonal_affinity_still_performs_only_reads(self):
        argv = [
            "-g",
            "rg",
            "-e",
            "san",
            "-v",
            "vg",
            "-n",
            "volume1",
            "--enable-zonal-affinity",
            "--dry-run",
        ]
        with mock.patch.object(connect, "check_az_cli_available"):
            with mock.patch.object(
                connect, "resolve_physical_zone", return_value="eastus-az1"
            ) as resolve_zone:
                with mock.patch.object(
                    connect,
                    "get_volume_storage_target",
                    return_value=("iqn.original", "portal.example", 3260),
                ):
                    with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
                        connect.main(argv)
        resolve_zone.assert_called_once()
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
