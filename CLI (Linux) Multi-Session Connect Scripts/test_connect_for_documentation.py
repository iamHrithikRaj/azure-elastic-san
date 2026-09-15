import importlib.util
import json
import os
import re
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
                "--elastic-san-name",
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
                "--elastic-san-name",
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
        resolver = mock.patch.object(
            connect, "resolve_target_vips", side_effect=AssertionError("opt-out DNS")
        )
        resolver.start()
        self.addCleanup(resolver.stop)
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
                "_get_zonal_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "_lookup_target_addresses", return_value=VIPS):
                    with mock.patch.object(connect, "_read_zonal_inventory", return_value=[]):
                        with mock.patch.object(connect, "connect_zonal_volume") as connector:
                            connect.connect_volumes(
                                "sub", "rg", "san", "vg", ["volume1"], 32, True
                            )

        decorated_iqn = "iqn.original:az-eastus-az3"
        resolve_physical_zone.assert_called_once_with("sub", "rg", "san")
        self.assertEqual(decorated_iqn, connector.call_args.args[0]["iqn"])
        self.assertEqual(32, len(connector.call_args.args[0]["slots"]))


VIPS = ["10.0.0.1", "10.0.0.2", "10.0.0.10"]
RAW_IQN = "iqn.2024-01.com.microsoft:volume"
IQN = RAW_IQN + ":az-eastus-az3"


def make_plan(vips=None, raw_iqn=RAW_IQN):
    vips = connect.canonicalize_vips(vips or VIPS)
    return dict(
        volume="volume1", raw_iqn=raw_iqn, iqn=raw_iqn + ":az-eastus-az3",
        hostname="portal.example", port=3260, vips=vips,
        slots=connect.allocate_vip_sessions(vips),
    )


class NativeIscsi(object):
    """An argv-level fake: no iscsiadm, sudo, DNS, or Azure commands can escape."""

    def __init__(self):
        self.nodes = []
        self.sessions = {}
        self.commands = []
        self.mutations = []
        self.login_hosts = []
        self.fail_at = None
        self.suppress_login = False
        self.unsigned_node_tpgt = False
        self.session_filter = lambda output: output

    def node(self, iqn, host, port, count=1):
        return {
            "node.name": iqn, "node.tpgt": "-1",
            "iface.iscsi_ifacename": "default", "iface.transport_name": "tcp",
            "node.conn[0].address": host, "node.conn[0].port": str(port),
            "node.startup": "manual", "node.session.nr_sessions": str(count),
            "node.conn[0].iscsi.HeaderDigest": "None",
            "node.conn[0].iscsi.DataDigest": "None",
        }

    def add_session(self, iqn, host, port=3260):
        sid = str(101 + 37 * len(self.sessions))
        self.sessions[sid] = dict(
            iqn=iqn, host=host, port=port, tpgt=7, healthy=True,
            persistent=host, disk_state="running", digest="CRC32C",
        )
        return sid

    def seed_layout(self, plan):
        for host in plan["vips"]:
            node = self.node(plan["iqn"], host, plan["port"], plan["slots"].count(host))
            node.update({
                "node.startup": "automatic",
                "node.conn[0].iscsi.HeaderDigest": "CRC32C",
                "node.conn[0].iscsi.DataDigest": "CRC32C",
            })
            self.nodes.append(node)
        for host in plan["slots"]:
            self.add_session(plan["iqn"], host, plan["port"])

    def session_output(self, sid):
        session = self.sessions[sid]
        # All sessions redirect to the same current endpoint. Only the
        # per-SID persistent portal can prove their original allocation.
        return self.session_filter("""
Target: {iqn} (non-flash)
    Current Portal: 192.168.50.50:3260,{tpgt}
    Persistent Portal: {portal},{tpgt}
        Iface Name: default
        Iface Transport: tcp
        SID: {sid}
        iSCSI Connection State: {connection}
        iSCSI Session State: LOGGED_IN
        Internal iscsid Session State: NO CHANGE
        HeaderDigest: {digest}
        DataDigest: {digest}
        Host Number: 8 State: running
        scsi8 Channel 00 Id 0 Lun: 0
            Attached scsi disk sdb State: {disk_state}
""".format(
            portal=connect.format_target_portal(session["persistent"], session["port"]),
            sid=sid, connection="LOGGED IN" if session["healthy"] else "IN LOGIN",
            **session
        ))

    def __call__(self, command, **kwargs):
        assert command[:3] == ["sudo", "-n", "iscsiadm"], command
        assert kwargs["timeout"] == connect.ISCSI_COMMAND_TIMEOUT_SECONDS
        assert kwargs["env"]["LC_ALL"] == "C"
        assert not kwargs.get("shell")
        self.commands.append(command)
        args = command[3:]

        def result(stdout="", code=0, stderr=""):
            return connect.subprocess.CompletedProcess(
                command, code, stdout.encode("utf-8"), stderr.encode("utf-8")
            )

        if args == ["-m", "session"]:
            if not self.sessions:
                return result(code=21, stderr="iscsiadm: No active sessions.")
            return result("\n".join(
                "tcp: [{}] {},{} {} (non-flash)".format(
                    sid, connect.format_target_portal(s["persistent"], s["port"]),
                    s["tpgt"], s["iqn"],
                ) for sid, s in self.sessions.items()
            ))
        if args == ["-m", "node"]:
            if not self.nodes:
                return result(code=21, stderr="iscsiadm: No records found")
            return result("\n".join(
                "{},{} {}".format(
                    connect.format_target_portal(n["node.conn[0].address"], n["node.conn[0].port"]),
                    "4294967295" if self.unsigned_node_tpgt and n["node.tpgt"] == "-1"
                    else n["node.tpgt"], n["node.name"],
                ) for n in self.nodes
            ))
        if args[:2] == ["-m", "session"] and args[-2:] == ["-P", "3"]:
            return result(self.session_output(args[3]))

        if args[:2] == ["-m", "node"]:
            iqn = args[args.index("--targetname") + 1]
            portal = args[args.index("--portal") + 1]
            host, port, _ = connect._parse_state_portal(
                portal if "," in portal else portal + ",-1"
            )
            matches = [
                n for n in self.nodes if n["node.name"] == iqn
                and n["node.conn[0].address"] == host
                and n["node.conn[0].port"] == str(port)
            ]
            if args[-2:] == ["--op", "show"]:
                return result("\n".join(
                    "# BEGIN RECORD\n" + "\n".join("{} = {}".format(k, v) for k, v in n.items())
                    + "\n# END RECORD" for n in matches
                ))

        self.mutations.append(command)
        if len(self.mutations) == self.fail_at:
            return result(code=15, stderr="native request rejected")
        if args[:2] == ["-m", "session"]:
            assert args[-2:] == ["--op", "new"], args
            seed = self.sessions[args[3]]
            self.login_hosts.append(seed["persistent"])
            if not self.suppress_login:
                self.add_session(seed["iqn"], seed["persistent"], seed["port"])
        elif args[-2:] == ["--op", "new"]:
            assert not matches, "Must not overwrite any existing node"
            # Exercise non-default global session-count/digest/startup defaults.
            self.nodes.append(self.node(iqn, host, port, count=8))
        elif "--login" in args:
            assert len(matches) == 1, args
            self.login_hosts.append(host)
            if not self.suppress_login:
                for _ in range(int(matches[0]["node.session.nr_sessions"])):
                    self.add_session(iqn, host, port)
        else:
            assert "--op" in args and args[args.index("--op") + 1] == "update", args
            assert len(matches) == 1, args
            matches[0][args[args.index("-n") + 1]] = args[args.index("-v") + 1]
        return result()


class ZonalNativeTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeIscsi()
        patches = {
            "resolve_physical_zone": mock.Mock(return_value="eastus-az3"),
            "_get_zonal_storage_target": mock.Mock(return_value=(RAW_IQN, "portal.example", 3260)),
            "_lookup_target_addresses": mock.Mock(return_value=VIPS),
            "connect_volume": mock.Mock(side_effect=AssertionError("legacy mutation")),
            "check_connection": mock.Mock(side_effect=AssertionError("legacy preflight")),
        }
        for name, replacement in patches.items():
            patcher = mock.patch.object(connect, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lookup = patches["_lookup_target_addresses"]
        self.discovery = patches["_get_zonal_storage_target"]
        patcher = mock.patch.object(connect.subprocess, "run", side_effect=self.native)
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(connect.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("builtins.print")
        self.print_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, volumes=None):
        connect.connect_volumes(None, "rg", "san", "vg", volumes or ["volume1"], 32, True)

    def test_actual_commands_use_numeric_sorted_ipv4_ipv6_and_mapped_vips(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "tests", "standalone_zonal_affinity_cases.json"
        )
        with open(fixture_path, encoding="utf-8") as fixtures:
            cases = json.load(fixtures)["addressCases"]
        for case in cases:
            if "expected" not in case:
                continue
            with self.subTest(case=case["name"]):
                native = NativeIscsi()
                self.run.side_effect = native
                self.lookup.return_value = case["addresses"]
                self.connect()
                plan = make_plan(case["addresses"])
                self.assertEqual(plan["slots"], native.login_hosts)
                self.assertEqual(32, len(native.sessions))
                self.assertEqual(["11", "11", "10"],
                                 [n["node.session.nr_sessions"] for n in native.nodes])
                self.assertTrue(all(n["node.startup"] == "automatic" for n in native.nodes))
                self.assertEqual(3, sum("--login" in c for c in native.mutations))
                clones = [c for c in native.mutations if c[3:5] == ["-m", "session"]]
                self.assertEqual(29, len(clones))
                self.assertEqual(["101", "138", "175"], [c[6] for c in clones[:3]])
                self.assertNotIn("portal.example", str(native.commands))
                self.assertNotIn("-4", str(native.commands))
                for command in native.mutations:
                    if command[3:5] == ["-m", "node"]:
                        self.assertEqual(IQN, command[command.index("--targetname") + 1])
                        self.assertEqual("default", command[command.index("--interface") + 1])
                        self.assertIn(
                            command[command.index("--portal") + 1],
                            [connect.format_target_portal(vip, 3260) for vip in case["expected"]],
                        )
                for key in ("HeaderDigest", "DataDigest"):
                    updates = [
                        c for c in native.mutations
                        if "node.conn[0].iscsi." + key in c
                    ]
                    self.assertEqual(3, len(updates))
                    self.assertTrue(all(c[-2:] == ["-v", "CRC32C"] for c in updates))
                self.assertTrue(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))

    def test_exact_healthy_persistent_layout_is_idempotent_including_redirects(self):
        self.native.seed_layout(make_plan())
        self.connect()
        self.assertEqual([], self.native.mutations)
        self.assertEqual(32, sum(c[-2:] == ["-P", "3"] for c in self.native.commands))
        self.assertTrue(any("Skipped; verified" in str(c) for c in self.print_mock.call_args_list))

    def test_known_variable_tpgt_matches_without_assuming_minus_one(self):
        self.native.seed_layout(make_plan())
        for node in self.native.nodes:
            node["node.tpgt"] = "7"
        self.connect()
        self.assertEqual([], self.native.mutations)

    def test_native_unsigned_unknown_node_tpgt_supports_fresh_and_existing_layout(self):
        self.native.unsigned_node_tpgt = True
        self.connect()
        self.assertEqual(32, len(self.native.sessions))
        mutations = list(self.native.mutations)
        self.connect()
        self.assertEqual(mutations, self.native.mutations)
        self.assertEqual([-1, -1, -1], [
            entry["portal"][2] for entry in connect._read_zonal_inventory("node")
        ])

    def test_unsigned_unknown_tpgt_is_only_accepted_in_flat_node_inventory(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "portal group"):
            connect._parse_state_portal("10.0.0.1:3260,4294967295")
        self.native.seed_layout(make_plan())
        self.native.unsigned_node_tpgt = True
        original = self.native

        def changed_node_config(command, **kwargs):
            result = original(command, **kwargs)
            if command[-2:] == ["--op", "show"]:
                result.stdout = result.stdout.replace(b"node.tpgt = -1", b"node.tpgt = 7")
            return result

        self.run.side_effect = changed_node_config
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
            self.connect()
        self.assertEqual([], self.native.mutations)

    def test_returned_nondefault_port_is_used_for_all_node_and_session_operations(self):
        self.discovery.return_value = (RAW_IQN, "portal.example", "4420")
        self.connect()
        self.assertTrue(all(n["node.conn[0].port"] == "4420" for n in self.native.nodes))
        self.assertTrue(all(s["port"] == 4420 for s in self.native.sessions.values()))
        for command in self.native.commands:
            if "--portal" in command:
                self.assertIn(":4420", command[command.index("--portal") + 1])

    def test_existing_layout_failures_never_mutate(self):
        def remove_session(native):
            native.sessions.pop(next(iter(native.sessions)))

        def change_session(native, **fields):
            next(iter(native.sessions.values())).update(fields)

        cases = {
            "partial": remove_session,
            "extra": lambda n: n.add_session(IQN, VIPS[0]),
            "unhealthy": lambda n: change_session(n, healthy=False),
            "disk-offline": lambda n: change_session(n, disk_state="offline"),
            "digest-unnegotiated": lambda n: change_session(n, digest="None"),
            "wrong-allocation": lambda n: change_session(n, host=VIPS[1], persistent=VIPS[1]),
            "undecorated": lambda n: change_session(n, iqn=RAW_IQN),
            "other-zone": lambda n: change_session(n, iqn=RAW_IQN + ":az-eastus-az1"),
            "wrong-port": lambda n: change_session(n, port=3261),
            "fqdn": lambda n: change_session(n, persistent="portal.example"),
            "only-current-portal": lambda n: change_session(n, persistent="192.168.50.50"),
            "tpgt-conflict": lambda n: n.nodes[0].update({"node.tpgt": "8"}),
            "manual": lambda n: n.nodes[0].update({"node.startup": "manual"}),
            "persist-count-minus-one": lambda n: n.nodes[0].update({"node.session.nr_sessions": "10"}),
            "persistent-digest": lambda n: n.nodes[0].update({"node.conn[0].iscsi.DataDigest": "None"}),
            "missing-persistent-setting": lambda n: n.nodes[0].pop("node.session.nr_sessions"),
            "custom-iface": lambda n: n.nodes[0].update({"iface.iscsi_ifacename": "custom"}),
            "extra-node": lambda n: n.nodes.append(n.node(IQN, "10.0.0.99", 3260)),
            "duplicate-node": lambda n: n.nodes.append(dict(n.nodes[0])),
            "no-nodes": lambda n: n.nodes.clear(),
            "no-sessions": lambda n: n.sessions.clear(),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.native = NativeIscsi()
                self.run.side_effect = self.native
                self.native.seed_layout(make_plan())
                change(self.native)
                with self.assertRaisesRegex(connect.ZonalAffinityError, "maintenance window"):
                    self.connect()
                self.assertEqual([], self.native.mutations)

    def test_legacy_or_other_zone_only_state_is_not_empty(self):
        for iqn in (RAW_IQN, RAW_IQN + ":az-eastus-az1"):
            for mode in ("sessions", "nodes"):
                with self.subTest(iqn=iqn, mode=mode):
                    self.native = NativeIscsi()
                    self.run.side_effect = self.native
                    if mode == "sessions":
                        self.native.add_session(iqn, "portal.example")
                    else:
                        self.native.nodes.append(self.native.node(iqn, "portal.example", 3260))
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "conflicting state"):
                        self.connect()
                    self.assertEqual([], self.native.mutations)

    def test_missing_or_ambiguous_persistent_portal_is_not_inferred_from_current(self):
        self.native.seed_layout(make_plan())
        for transform in (
            lambda s: re.sub(r"^.*Persistent Portal:.*\n", "", s, flags=re.M),
            lambda s: s + "\nPersistent Portal: 10.0.0.1:3260,7\n",
            lambda s: s.replace("Persistent Portal: 10.0.0.1", "Persistent Portal: 10.0.0.2"),
        ):
            with self.subTest(transform=transform):
                self.native.session_filter = transform
                with self.assertRaisesRegex(connect.ZonalAffinityError, "portal|Portal|VIP"):
                    self.connect()
                self.assertEqual([], self.native.mutations)

    def test_second_volume_dns_failure_prevents_all_native_commands(self):
        self.discovery.side_effect = [
            (RAW_IQN, "one.example", 3260),
            (RAW_IQN + "2", "two.example", 3260),
        ]
        self.lookup.side_effect = [VIPS, OSError("DNS unavailable"), OSError("DNS unavailable"),
                                   OSError("DNS unavailable")]
        with self.assertRaisesRegex(connect.VipResolutionError, "two.example"):
            self.connect(["volume1", "volume2"])
        self.run.assert_not_called()

    def test_second_volume_existing_conflict_prevents_first_volume_mutations(self):
        other = RAW_IQN + "2"
        self.discovery.side_effect = [
            (RAW_IQN, "portal.example", 3260), (other, "portal.example", 3260),
        ]
        self.native.add_session(other, "portal.example")
        with self.assertRaisesRegex(connect.ZonalAffinityError, "conflicting state"):
            self.connect(["volume1", "volume2"])
        self.assertEqual([], self.native.mutations)
        self.lookup.assert_called_once_with("portal.example")

    def test_second_volume_persistent_configuration_is_checked_before_first_mutation(self):
        other = RAW_IQN + "2"
        self.discovery.side_effect = [
            (RAW_IQN, "portal.example", 3260), (other, "portal.example", 3260),
        ]
        self.native.seed_layout(make_plan(raw_iqn=other))
        self.native.nodes[-1]["node.startup"] = "manual"
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node configuration"):
            self.connect(["volume1", "volume2"])
        self.assertEqual([], self.native.mutations)
        self.assertEqual(3, sum(c[-2:] == ["--op", "show"] for c in self.native.commands))

    def test_all_iqn_and_port_validation_precedes_dns(self):
        for target in (
            ("iqn.bad --logout", "portal.example", 3260),
            (RAW_IQN + "2", "portal.example", 0),
            (RAW_IQN + "2", "portal.example", 65536),
            (RAW_IQN + ":az-eastus-az3", "portal.example", 3260),
            ("iqn." + "a" * 220, "portal.example", 3260),
        ):
            with self.subTest(target=target):
                self.discovery.side_effect = [(RAW_IQN, "one.example", 3260), target]
                with self.assertRaises((connect.ZonalAffinityError, connect.VipResolutionError)):
                    self.connect(["volume1", "volume2"])
                self.lookup.assert_not_called()
                self.run.assert_not_called()

    def test_zone_validation_precedes_discovery_and_dns(self):
        connect.resolve_physical_zone.side_effect = connect.ZonalAffinityError("region mismatch")
        with self.assertRaisesRegex(connect.ZonalAffinityError, "region mismatch"):
            self.connect()
        self.discovery.assert_not_called()
        self.lookup.assert_not_called()
        self.run.assert_not_called()

    def test_duplicate_volume_and_iqn_are_deduplicated_safely(self):
        self.connect(["volume1", "volume1", "alias"])
        self.assertEqual(2, self.discovery.call_count)
        self.assertEqual(32, len(self.native.sessions))
        self.assertEqual(3, len(self.native.nodes))
        self.lookup.assert_called_once_with("portal.example")

    def test_conflicting_duplicate_iqn_fails_before_dns(self):
        self.discovery.side_effect = [
            (RAW_IQN, "one.example", 3260), (RAW_IQN, "two.example", 3260),
        ]
        with self.assertRaisesRegex(connect.ZonalAffinityError, "Conflicting portal"):
            self.connect(["volume1", "alias"])
        self.lookup.assert_not_called()
        self.run.assert_not_called()

    def test_fqdn_cache_is_shared_only_within_invocation(self):
        self.discovery.side_effect = lambda sub, rg, san, vg, name: (
            RAW_IQN + name, "portal.example", 3260,
        )
        self.connect(["one", "two"])
        self.assertEqual(64, len(self.native.sessions))
        self.lookup.assert_called_once_with("portal.example")
        mutations = len(self.native.mutations)
        self.connect(["one", "two"])
        self.assertEqual(2, self.lookup.call_count)
        self.assertEqual(mutations, len(self.native.mutations))

    def test_native_failure_stops_and_retry_refuses_partial_state_without_cleanup(self):
        self.native.fail_at = 20
        with self.assertRaisesRegex(connect.ZonalAffinityError, "exit 15.*native request rejected"):
            self.connect()
        self.assertGreater(len(self.native.sessions), 0)
        self.assertEqual(20, len(self.native.mutations))
        self.assertFalse(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))
        self.native.fail_at = None
        with self.assertRaisesRegex(connect.ZonalAffinityError, "No automatic rollback"):
            self.connect()
        self.assertEqual(20, len(self.native.mutations))
        self.assertFalse(any("--logout" in c or "delete" in c for c in self.native.commands))

    def test_successful_request_without_session_is_not_reported_as_ready(self):
        self.native.suppress_login = True
        with self.assertRaisesRegex(connect.ZonalAffinityError, "Timed out.*healthy session"):
            self.connect()
        self.assertEqual(1, len(self.native.login_hosts))
        self.assertEqual(connect.ISCSI_READY_ATTEMPTS - 1, self.sleep.call_count)
        self.assertTrue(all(n["node.startup"] == "manual" for n in self.native.nodes))
        self.assertFalse(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))

    def test_new_sessions_are_waited_for_until_healthy(self):
        seen = set()

        def initially_unhealthy(output):
            sid = re.search(r"SID: (\d+)", output).group(1)
            if sid not in seen:
                seen.add(sid)
                return output.replace("State: LOGGED IN", "State: IN LOGIN")
            return output

        self.native.session_filter = initially_unhealthy
        self.connect()
        self.assertEqual(32, len(seen))
        self.assertEqual(32, self.sleep.call_count)
        self.assertEqual(32, len(self.native.sessions))

    def test_extra_session_from_login_is_rejected_instead_of_treated_as_seed(self):
        def run(command, **kwargs):
            result = self.native(command, **kwargs)
            if "--login" in command:
                self.native.add_session(IQN, VIPS[0])
            return result

        self.run.side_effect = run
        with self.assertRaisesRegex(connect.ZonalAffinityError, "changed unexpectedly"):
            self.connect()
        self.assertEqual(1, len(self.native.login_hosts))
        self.assertFalse(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))

    def test_missing_persistence_despite_successful_update_is_not_success(self):
        def run(command, **kwargs):
            result = self.native(command, **kwargs)
            if "node.session.nr_sessions" in command and command[-1] == "11":
                self.native.nodes[0]["node.session.nr_sessions"] = "10"
            return result

        self.run.side_effect = run
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node configuration"):
            self.connect()
        self.assertEqual(32, len(self.native.sessions))
        self.assertFalse(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))

    def test_unrelated_existing_targets_are_neither_modified_nor_used_as_seeds(self):
        other = "iqn.2024-01.example:unrelated"
        old_sid = self.native.add_session(other, "old.example")
        old_node = self.native.node(other, "old.example", 3260)
        self.native.nodes.append(old_node)
        self.connect()
        self.assertEqual(old_node, self.native.nodes[0])
        self.assertEqual(33, len(self.native.sessions))
        self.assertEqual(other, self.native.sessions[old_sid]["iqn"])
        for command in self.native.mutations:
            self.assertNotIn(other, command)
            if "-r" in command:
                self.assertNotEqual(old_sid, command[command.index("-r") + 1])

    def test_inventory_errors_and_unrecognized_output_are_not_empty_state(self):
        for code, stdout, stderr in (
            (21, "", "permission denied"), (1, "", "No active sessions."),
            (0, "", ""), (0, "unexpected output", ""),
            (21, "tcp: [1] invalid iqn.test", "No active sessions."),
        ):
            with self.subTest(code=code, stdout=stdout):
                self.run.side_effect = None
                self.run.return_value = connect.subprocess.CompletedProcess(
                    [], code, stdout.encode(), stderr.encode()
                )
                with self.assertRaises(connect.ZonalAffinityError):
                    self.connect()
                self.assertEqual([], self.native.mutations)

    def test_native_timeout_and_start_failure_include_recovery_guidance(self):
        for error in (
            connect.subprocess.TimeoutExpired(["sudo", "-n", "iscsiadm"], 30),
            OSError("not installed"),
        ):
            with self.subTest(error=error):
                self.run.side_effect = error
                with self.assertRaisesRegex(connect.ZonalAffinityError, "No automatic rollback"):
                    self.connect()
                self.assertEqual([], self.native.mutations)


class VipNormalizationTests(unittest.TestCase):
    def test_nondecimal_or_short_dotted_addresses_are_rejected_before_ip_parser(self):
        for address in (
            "010.0.0.1", "10.01.0.1", "10.0.0.001", "10.1",
            "0x0a.0.0.1", "::ffff:010.0.0.1", "::ffff:10.1",
            "::ffff:10.0.0.001",
        ):
            with self.subTest(address=address):
                with mock.patch("ipaddress.ip_address") as parser:
                    with self.assertRaisesRegex(connect.VipResolutionError, "invalid IP address"):
                        connect.canonicalize_vips([address, "10.0.0.2", "10.0.0.3"])
                    parser.assert_not_called()


class ZonalArgumentTests(unittest.TestCase):
    def invoke_main(self, arguments):
        with mock.patch.object(connect, "check_iscsi"):
            with mock.patch.object(connect, "check_mpio"):
                connect.main(["-g", "rg", "-e", "san", "-v", "vg", "-n", "volume1"] + arguments)

    def test_enabled_rejects_explicit_non32_before_legacy_clamping(self):
        for count in ("0", "-1", "1", "4", "31", "33", "64", "not-a-number"):
            with self.subTest(count=count):
                with mock.patch.object(connect, "connect_volumes") as connector:
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "exactly 32"):
                        self.invoke_main(["--enable-zonal-affinity", "-s", count])
                    connector.assert_not_called()

    def test_enabled_default_and_explicit32_and_legacy_counts_are_preserved(self):
        cases = [
            (["--enable-zonal-affinity"], 32, True),
            (["--enable-zonal-affinity", "-s", "32"], 32, True),
            ([], 32, False), (["-s", "4"], 4, False),
            (["-s", "33"], 32, False), (["-s", "0"], 0, False),
        ]
        for arguments, count, enabled in cases:
            with self.subTest(arguments=arguments):
                with mock.patch.object(connect, "connect_volumes") as connector:
                    self.invoke_main(arguments)
                self.assertEqual((count, enabled), connector.call_args.args[-2:])

    def test_enabled_direct_call_rejects_non32_before_mapping(self):
        with mock.patch.object(connect, "resolve_physical_zone") as mapping:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "exactly 32"):
                connect.connect_volumes(
                    "sub", "rg", "san", "vg", ["volume1"], 4, True
                )
        mapping.assert_not_called()

    def test_empty_selection_is_not_success(self):
        with mock.patch.object(connect, "resolve_physical_zone") as mapping:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "At least one volume"):
                connect.build_zonal_plan(None, "rg", "san", "vg", [], 32)
        mapping.assert_not_called()

    def test_enabled_discovery_uses_bounded_argv_path_and_preserves_resource_names(self):
        payload = json.dumps({
            "targetIqn": RAW_IQN, "targetPortalHostname": "portal.example",
            "targetPortalPort": 3260,
        })
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run:
            target = connect._get_zonal_storage_target(
                "subscription name", "group name", "san", "volume group", "volume name"
            )
        self.assertEqual((RAW_IQN, "portal.example", 3260), target)
        run.assert_called_once_with([
            "az", "elastic-san", "volume", "show", "-g", "group name",
            "-e", "san", "-v", "volume group", "-n", "volume name",
            "--query", "storageTarget", "--output", "json",
            "--subscription", "subscription name",
        ], "Volume target lookup")

    def test_enabled_discovery_rejects_invalid_target_payloads(self):
        for payload in ("not json", "[]", "null", "{}"):
            with self.subTest(payload=payload):
                with mock.patch.object(connect, "_run_az_command", return_value=payload):
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "Invalid volume"):
                        connect._get_zonal_storage_target(None, "rg", "san", "vg", "v")


if __name__ == "__main__":
    unittest.main()
