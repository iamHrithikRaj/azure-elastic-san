"""Offline Linux VIP contract tests; never invoke an initiator or Azure."""

import builtins
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_connect_for_documentation import completed_process, connect, locations_payload


VIPS = ["10.0.0.1", "10.0.0.2", "10.0.0.10"]
RAW_IQN = "iqn.2024-01.com.microsoft:volume"
IQN = RAW_IQN + ":az-eastus-az3"
with open(os.path.join(os.path.dirname(__file__), "vip_address_cases.json")) as fixtures:
    ADDRESS_CASES = json.load(fixtures)["addressCases"]


def make_plan(vips=None, raw_iqn=RAW_IQN):
    vips = connect.canonicalize_vips(vips or VIPS)
    return dict(
        volume="volume1", raw_iqn=raw_iqn, iqn=raw_iqn + ":az-eastus-az3",
        hostname="portal.example", port=3260, vips=vips,
        slots=connect.allocate_vip_sessions(vips),
    )


def make_nodes(plan):
    nodes = []
    for host in plan["vips"]:
        nodes.append(dict(iqn=plan["iqn"], portal=(host, plan["port"], -1), fields={
            "node.name": plan["iqn"], "node.tpgt": "-1",
            "iface.iscsi_ifacename": "default", "iface.transport_name": "tcp",
            "node.conn[0].address": host, "node.conn[0].port": str(plan["port"]),
            "node.startup": "automatic", "node.conn[0].startup": "manual",
            "node.session.nr_sessions": str(plan["slots"].count(host)),
            "node.conn[0].iscsi.HeaderDigest": "CRC32C",
            "node.conn[0].iscsi.DataDigest": "CRC32C",
        }))
    return nodes


class LayoutProofTests(unittest.TestCase):
    def setUp(self):
        self.plan = make_plan()
        self.nodes = make_nodes(self.plan)
        self.sessions = [
            dict(sid=str(101 + i * 37), iqn=IQN, portal=(host, 3260, 7),
                 current_portal=("192.168.50.50", 4420), healthy=True)
            for i, host in enumerate(self.plan["slots"])
        ]

    def check(self):
        return connect.check_zonal_layout(self.plan, self.sessions, self.nodes)

    def test_only_empty_or_complete_layout_is_supported(self):
        self.assertTrue(self.check())
        self.assertFalse(connect.check_zonal_layout(self.plan, [], []))
        for sessions, nodes in (
            ([], self.nodes), (self.sessions, []), (self.sessions[:-1], self.nodes),
            (self.sessions + [self.sessions[0]], self.nodes), (self.sessions, self.nodes[:2]),
            (self.sessions, self.nodes + [self.nodes[0]]),
        ):
            with self.subTest(sessions=len(sessions), nodes=len(nodes)):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "conflicting state"):
                    connect.check_zonal_layout(self.plan, sessions, nodes)

    def test_each_original_portal_and_session_identity_is_required(self):
        for update in (
            {"portal": (VIPS[1], 3260, 7)}, {"portal": ("192.168.50.50", 3260, 7)},
            {"portal": ("portal.example", 3260, 7)}, {"portal": (VIPS[0], 4420, 7)},
            {"iqn": RAW_IQN}, {"iqn": RAW_IQN + ":az-eastus-az1"},
            {"sid": self.sessions[1]["sid"]}, {"healthy": False},
        ):
            with self.subTest(update=update):
                sessions = copy.deepcopy(self.sessions)
                sessions[0].update(update)
                with self.assertRaises(connect.ZonalAffinityError):
                    connect.check_zonal_layout(self.plan, sessions, self.nodes)

    def test_full_persistence_and_both_startup_fields_are_proved(self):
        for key, bad in (
            ("node.session.nr_sessions", "10"), ("node.startup", "manual"),
            ("node.conn[0].startup", "automatic"), ("iface.iscsi_ifacename", "custom"),
            ("iface.transport_name", "iser"), ("node.conn[0].address", "portal.example"),
            ("node.conn[0].port", "3261"), ("node.name", RAW_IQN),
            ("node.conn[0].iscsi.HeaderDigest", "None"),
            ("node.conn[0].iscsi.DataDigest", "None"),
            ("node.tpgt", "4294967295"),
        ):
            with self.subTest(key=key):
                nodes = copy.deepcopy(self.nodes)
                nodes[0]["fields"][key] = bad
                with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
                    connect.check_zonal_layout(self.plan, self.sessions, nodes)
                del nodes[0]["fields"][key]
                with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
                    connect.check_zonal_layout(self.plan, self.sessions, nodes)

    def test_unsigned_tpgt_is_flat_only_and_requires_signed_confirmation(self):
        for separator in (" ", "   ", "\t", " \t "):
            with self.subTest(separator=separator):
                nodes = connect._parse_zonal_nodes("\n".join(
                    "  {}:3260,4294967295{}{}  ".format(host, separator, IQN) for host in VIPS
                ))
                self.assertEqual([-1, -1, -1], [n["portal"][2] for n in nodes])
                for node, reference in zip(nodes, self.nodes):
                    node["fields"] = connect._parse_zonal_node_config(
                        "\n# BEGIN RECORD\n" + "\n".join(
                            " \t{} \t=  {}\t ".format(k, v)
                            for k, v in reference["fields"].items()
                        ) + "\n# END RECORD\n"
                    )
                self.assertTrue(connect.check_zonal_layout(self.plan, self.sessions, nodes))
                nodes[0]["fields"]["node.tpgt"] = "7"
                with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
                    connect.check_zonal_layout(self.plan, self.sessions, nodes)
        with self.assertRaisesRegex(connect.ZonalAffinityError, "portal group"):
            connect._parse_state_portal("10.0.0.1:3260,4294967295")

    def test_known_tpgt_must_match_sessions(self):
        for node in self.nodes:
            node["portal"] = node["portal"][:2] + (7,)
            node["fields"]["node.tpgt"] = "7"
        self.assertTrue(self.check())
        self.nodes[0]["portal"] = (VIPS[0], 3260, 8)
        self.nodes[0]["fields"]["node.tpgt"] = "8"
        with self.assertRaisesRegex(connect.ZonalAffinityError, "mismatched session"):
            self.check()

    def test_extra_persistent_connection_is_not_supported(self):
        self.nodes[0]["fields"]["node.conn[1].address"] = VIPS[0]
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
            self.check()

    def test_ambiguous_or_malformed_records_never_prove_layout(self):
        for output in ("node.name = " + IQN + "\nnode.name = " + IQN, "= missing-key", "not a setting"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "Ambiguous"):
                    connect._parse_zonal_node_config(output)
        for output in ("unknown format", "10.0.0.1:3260,7 " + IQN + " extra",
                       "10.0.0.1:3260,4294967296 " + IQN,
                       "10.0.0.1:0,-1 " + IQN):
            with self.subTest(output=output):
                with self.assertRaises(connect.ZonalAffinityError):
                    connect._parse_zonal_nodes(output)

    def test_unrelated_state_is_not_adopted_or_counted(self):
        self.sessions.append(dict(sid="9999", iqn="iqn.unrelated"))
        self.nodes.append(dict(iqn="iqn.unrelated"))
        self.assertTrue(self.check())
        self.assertFalse(connect.check_zonal_layout(self.plan, self.sessions[-1:], self.nodes[-1:]))

    def test_unrelated_loopback_and_link_local_node_records_remain_visible(self):
        for host in ("127.0.0.1", "169.254.10.1", "::1", "fe80::1"):
            with self.subTest(host=host):
                node = connect._parse_zonal_nodes(
                    connect.format_target_portal(host, 3260) + ",1 iqn.unrelated"
                )[0]
                self.assertEqual(host, node["portal"][0])
                self.assertTrue(connect.check_zonal_layout(
                    self.plan, self.sessions, self.nodes + [node]
                ))

    def test_native_scoped_and_unbracketed_dotted_ipv6_inventory(self):
        for text, expected in (
            ("[fe80::1%eth0]:3260,1", "fe80::1%eth0"),
            ("fe80::1%eth0.42:3260,1", "fe80::1%eth0.42"),
            ("::ffff:10.0.0.1:3260,1", "10.0.0.1"),
            ("2001:db8::10.0.0.1:3260,1", "2001:db8::a00:1"),
        ):
            with self.subTest(text=text):
                node = connect._parse_zonal_nodes(text + " iqn.unrelated")[0]
                self.assertEqual((expected, 3260, 1), node["portal"])
                self.assertTrue(connect.check_zonal_layout(
                    self.plan, self.sessions, self.nodes + [node]
                ))
                self.assertFalse(connect.check_zonal_layout(self.plan, [], [node]))
        for text in ("[fe80::1%]:3260,1", "[fe80::1%a%b]:3260,1",
                     "[10.0.0.1%eth0]:3260,1", "fd00::1:3260,1"):
            with self.subTest(text=text):
                with self.assertRaises(connect.ZonalAffinityError):
                    connect._parse_zonal_nodes(text + " iqn.unrelated")
        with self.assertRaises(connect.ZonalAffinityError):
            connect._parse_state_portal("::ffff:10.0.0.1:3260,1")
        scoped = connect._parse_zonal_nodes("[fe80::1%eth0]:3260,1 " + IQN)[0]
        with self.assertRaisesRegex(connect.ZonalAffinityError, "conflicting state"):
            connect.check_zonal_layout(self.plan, self.sessions, self.nodes + [scoped])


class VipPlanningTests(unittest.TestCase):
    def test_address_corpus_and_exact_session_assignment(self):
        for case in ADDRESS_CASES:
            with self.subTest(case=case["name"]):
                if case.get("error"):
                    with self.assertRaises(connect.VipResolutionError):
                        connect.canonicalize_vips(case["addresses"])
                else:
                    expected = case["expected"]
                    self.assertEqual(expected, connect.canonicalize_vips(case["addresses"]))
                    slots = connect.allocate_vip_sessions(case["addresses"])
                    self.assertEqual([expected[i % 3] for i in range(32)], slots)
                    self.assertEqual([11, 11, 10], [slots.count(ip) for ip in expected])

    def test_invalid_extra_addresses_are_not_filtered(self):
        for extra in (
            None, 123, "", "fd00::1%eth0", "10.0.0.1\n", "::ffff:0.0.0.0",
            "::ffff:169.254.1.1", "::ffff:224.0.0.1", "::ffff:255.255.255.255",
            "10.01.0.1", "::ffff:10.0.0.001", "0x0a.0.0.1", "10.1",
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(connect.VipResolutionError):
                    connect.canonicalize_vips(VIPS + [extra])

    def test_retry_success_uses_one_answer_and_defensive_invocation_cache(self):
        cache = {}
        with mock.patch.object(connect, "_lookup_target_addresses", side_effect=[
            OSError("temporary failure"), ["10.0.0.8"], ["fd00::1"] + VIPS[:2],
        ]) as lookup, mock.patch.object(connect.time, "sleep") as sleep:
            first = connect.resolve_target_vips("portal.example", cache)
            self.assertEqual(["10.0.0.1", "10.0.0.2", "fd00::1"], first)
            first.clear()
            self.assertEqual(["10.0.0.1", "10.0.0.2", "fd00::1"],
                             connect.resolve_target_vips("PORTAL.example", cache))
        self.assertEqual(3, lookup.call_count)
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_rotating_answers_and_timeouts_never_union_or_cache_failure(self):
        for responses in (
            [["10.0.0.1"], ["10.0.0.2"], ["10.0.0.3"]],
            [subprocess.TimeoutExpired("DNS", 5)] * 3,
            [VIPS + ["bad"]] * 3,
        ):
            with self.subTest(responses=responses):
                cache = {}
                with mock.patch.object(connect, "_lookup_target_addresses", side_effect=responses) as lookup:
                    with mock.patch.object(connect.time, "sleep") as sleep:
                        with self.assertRaisesRegex(connect.VipResolutionError, "after 3 attempts"):
                            connect.resolve_target_vips("portal.example", cache)
                self.assertEqual(3, lookup.call_count)
                self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)
                self.assertEqual({}, cache)

    def test_resolver_is_local_bounded_and_receives_hostname_as_one_argument(self):
        result = subprocess.CompletedProcess([], 0, b'["10.0.0.1"]', b"")
        with mock.patch.object(connect.subprocess, "run", return_value=result) as run:
            self.assertEqual(["10.0.0.1"], connect._lookup_target_addresses("portal.example"))
        args, kwargs = run.call_args
        self.assertEqual(sys.executable, args[0][0])
        self.assertEqual("portal.example", args[0][-1])
        self.assertEqual(5, kwargs["timeout"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertNotIn("shell", kwargs)
        self.assertIn("socket.getaddrinfo", args[0][2])

    def test_resolver_rejects_invalid_output_errors_and_warnings(self):
        for code, out, err in ((1, b"[]", b"failed"), (0, b"[]", b"warning"), (0, b"?", b"")):
            with self.subTest(code=code, out=out, err=err):
                with mock.patch.object(connect.subprocess, "run", return_value=
                                       subprocess.CompletedProcess([], code, out, err)):
                    with self.assertRaises((OSError, ValueError)):
                        connect._lookup_target_addresses("portal.example")

    def test_invalid_hostnames_never_reach_resolver(self):
        with mock.patch.object(connect, "_lookup_target_addresses") as lookup:
            for hostname in ("", "...", "-portal", "portal;exit", "portal\n", None):
                with self.subTest(hostname=hostname):
                    with self.assertRaises(connect.VipResolutionError):
                        connect.resolve_target_vips(hostname)
        lookup.assert_not_called()

    def test_strict_session_count_and_host_port_format(self):
        for count in (0, 1, 31, 33, "32", 32.0, True, None):
            with self.subTest(count=count):
                with self.assertRaises(connect.VipResolutionError):
                    connect.allocate_vip_sessions(VIPS, count)
        self.assertEqual("[fd00::1]:3260", connect.format_target_portal("fd00::1", 3260))
        self.assertEqual("10.0.0.1:3260", connect.format_target_portal("10.0.0.1", "3260"))
        for port in (0, -1, 65536, True, "3;exit", 3260.5, "3260\n", "\u0663"):
            with self.subTest(port=port):
                with self.assertRaises(connect.VipResolutionError):
                    connect.validate_target_port(port)

    def test_all_inputs_and_iqns_precede_dns(self):
        for failure in (("IQN.bad", "portal.example", 3260),
                        ("i" * 223, "portal.example", 3260),
                        (RAW_IQN + ":az-eastus-az3", "portal.example", 3260)):
            with self.subTest(failure=failure):
                with mock.patch.object(connect, "resolve_zonal_affinity_context", return_value=("sub", "eastus-az3")):
                    with mock.patch.object(connect, "get_mapped_volume_target", side_effect=[
                        (RAW_IQN, "portal.example", 3260), failure,
                    ]):
                        with mock.patch.object(connect, "_lookup_target_addresses") as lookup:
                            with self.assertRaises(connect.ZonalAffinityError):
                                connect.build_zonal_plan(None, "rg", "san", "vg", ["one", "two"], 32)
                lookup.assert_not_called()

    def test_plan_deduplicates_aliases_and_pins_subscription(self):
        with mock.patch.object(connect, "resolve_zonal_affinity_context", return_value=("pinned-sub", "eastus-az3")) as context:
            with mock.patch.object(connect, "get_mapped_volume_target", return_value=(RAW_IQN, "portal.example", 3260)) as target:
                with mock.patch.object(connect, "_lookup_target_addresses", return_value=VIPS) as lookup:
                    plans = connect.build_zonal_plan(None, "rg", "san", "vg", ["one", "one", "alias"], 32)
        context.assert_called_once_with(None, "rg", "san")
        self.assertEqual([
            mock.call("pinned-sub", "rg", "san", "vg", "one"),
            mock.call("pinned-sub", "rg", "san", "vg", "alias"),
        ], target.call_args_list)
        lookup.assert_called_once_with("portal.example")
        self.assertEqual(1, len(plans))
        self.assertEqual(IQN, plans[0]["iqn"])
        self.assertEqual([11, 11, 10], [plans[0]["slots"].count(vip) for vip in VIPS])

    def test_conflicting_alias_refuses_before_dns(self):
        with mock.patch.object(connect, "resolve_zonal_affinity_context", return_value=("sub", "eastus-az3")):
            with mock.patch.object(connect, "get_mapped_volume_target", side_effect=[
                (RAW_IQN, "one.example", 3260), (RAW_IQN, "two.example", 3260),
            ]):
                with mock.patch.object(connect, "_lookup_target_addresses") as lookup:
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "Conflicting portal"):
                        connect.build_zonal_plan(None, "rg", "san", "vg", ["one", "alias"], 32)
        lookup.assert_not_called()

    def test_invalid_count_or_selection_prevents_discovery(self):
        with mock.patch.object(connect, "preflight_zonal_affinity") as mapping:
            for names, count in (([], 32), (["one"], 4), ([""], 32), ([None], 32)):
                with self.subTest(names=names, count=count):
                    with self.assertRaises(connect.ZonalAffinityError):
                        connect.build_zonal_plan(None, "rg", "san", "vg", names, count)
        mapping.assert_not_called()


class SysfsFixture:
    """Real attribute files/directories, with class links emulated on Windows."""

    def __init__(self, base):
        base = os.path.realpath(base)
        self.base = base
        self.root = os.path.join(base, "class")
        self.devices = os.path.join(base, "devices", "platform")
        self.links = {}
        self.paths = {}
        for name in ("iscsi_session", "iscsi_connection", "scsi_host", "scsi_device", "block"):
            os.makedirs(os.path.join(self.root, name))

    def actual(self, path):
        # Linux's :<CID> / H:C:T:L names are not legal NTFS directory names.
        path = os.fspath(path)
        if path.startswith(self.base):
            return self.base + path[len(self.base):].replace(":", "!")
        return path

    def write(self, path, value):
        path = self.actual(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(str(value) + "\n")

    def add(self, sid, iqn=IQN, host=VIPS[0], port=3260, disk=True):
        sid = str(sid)
        host_number = str(int(sid) + 500)
        session_name, connection_name = "session" + sid, "connection" + sid + ":0"
        host_device = os.path.join(self.devices, "host" + host_number)
        device = os.path.join(host_device, session_name)
        connection_device = os.path.join(device, connection_name)
        os.makedirs(self.actual(connection_device))
        session = os.path.join(self.root, "iscsi_session", session_name)
        connection = os.path.join(self.root, "iscsi_connection", connection_name)
        host_path = os.path.join(self.root, "scsi_host", "host" + host_number)
        self.links[os.path.join(session, "device")] = device
        self.links[os.path.join(connection, "device")] = connection_device
        self.links[os.path.join(host_path, "device")] = host_device
        for key, value in {"targetname": iqn, "ifacename": "default", "tpgt": "7", "state": "LOGGED_IN"}.items():
            self.write(os.path.join(session, key), value)
        for key, value in {
            "persistent_address": host, "persistent_port": port,
            "address": "192.168.50.50", "port": 4420,
            "header_digest": "1", "data_digest": "1", "state": "up",
        }.items():
            self.write(os.path.join(connection, key), value)
        for key, value in {"proc_name": "iscsi_tcp", "state": "running"}.items():
            self.write(os.path.join(host_path, key), value)
        self.paths[sid] = dict(
            session=session, connection=connection, host=host_path,
            device=device, host_number=host_number,
        )
        if disk:
            self.add_disk(sid)
        return self.paths[sid]

    def add_disk(self, sid, lun=0):
        paths = self.paths[str(sid)]
        target = paths["host_number"] + ":0:0"
        lun_name = target + ":" + str(lun)
        lun_path = os.path.join(paths["device"], "target" + target, lun_name)
        class_lun = os.path.join(self.root, "scsi_device", lun_name, "device")
        disk_name = "sd" + str(sid) + "_" + str(lun)
        disk_path = os.path.join(lun_path, "block", disk_name)
        class_block = os.path.join(self.root, "block", disk_name)
        os.makedirs(self.actual(disk_path))
        self.links[class_lun] = lun_path
        self.links[class_block] = disk_path
        self.links[os.path.join(class_block, "device")] = lun_path
        self.write(os.path.join(class_lun, "state"), "running")
        paths["lun"], paths["disk"] = class_lun, class_block

    def patch(self, case):
        realpath = os.path.realpath
        listdir, stat, open_file = os.listdir, os.stat, builtins.open
        for patcher in (
            mock.patch.object(connect, "SYSFS_CLASS_ROOT", self.root),
            mock.patch.object(connect.os.path, "realpath", side_effect=lambda path: self.links.get(path, realpath(path))),
            mock.patch.object(connect.os, "listdir", side_effect=lambda path: [
                name.replace("!", ":") for name in listdir(self.actual(path))
            ]),
            mock.patch.object(connect.os, "stat", side_effect=lambda path, **kwargs: stat(self.actual(path), **kwargs)),
            mock.patch.object(builtins, "open", side_effect=lambda path, *args, **kwargs: open_file(
                self.actual(path), *args, **kwargs
            )),
        ):
            patcher.start()
            case.addCleanup(patcher.stop)


class SysfsProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fs = SysfsFixture(temporary.name)
        self.fs.patch(self)
        self.plan = make_plan()
        self.no_native = mock.patch.object(connect.subprocess, "Popen", side_effect=AssertionError("native probe"))
        self.no_native.start()
        self.addCleanup(self.no_native.stop)

    def read(self, pending=False):
        return connect._read_zonal_sessions([self.plan], allow_pending=pending)

    def test_per_sid_original_portals_survive_converged_current_redirects(self):
        for index, vip in enumerate(self.plan["slots"]):
            self.fs.add(101 + index * 37, host=vip)
        entries = self.read()
        self.assertEqual(32, len(entries))
        self.assertEqual([11, 11, 10], [
            sum(e["portal"][0] == vip for e in entries) for vip in VIPS
        ])
        self.assertTrue(all(e["current_portal"] == ("192.168.50.50", 4420) for e in entries))
        self.assertTrue(all(e["healthy"] for e in entries))
        self.assertEqual(32, len({e["identity"][1] for e in entries}))
        self.assertTrue(connect.check_zonal_layout(self.plan, entries, make_nodes(self.plan)))

    def test_empty_class_inventory_and_unrelated_sessions_do_not_require_native_probe(self):
        self.assertEqual([], self.read())
        paths = self.fs.add(100, iqn="iqn.unrelated", host="old.example")
        self.fs.write(os.path.join(paths["host"], "proc_name"), "unrelated_transport")
        self.assertEqual([], self.read())

    def test_missing_persistent_attributes_never_use_current_values(self):
        paths = self.fs.add(100)
        for field in ("persistent_address", "persistent_port"):
            path = os.path.join(paths["connection"], field)
            with open(path) as handle:
                original = handle.read()
            os.remove(self.fs.actual(path))
            with self.subTest(field=field):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "Cannot establish"):
                    self.read()
            self.fs.write(path, original.strip())

    def test_wrong_links_and_nondefault_topology_refuse(self):
        paths = self.fs.add(100)
        for key in (
            os.path.join(paths["session"], "device"),
            os.path.join(paths["connection"], "device"),
            os.path.join(paths["host"], "device"),
            paths["lun"], paths["disk"], os.path.join(paths["disk"], "device"),
        ):
            with self.subTest(link=key):
                original = self.fs.links[key]
                self.fs.links[key] = self.fs.root
                with self.assertRaises(connect.ZonalAffinityError):
                    self.read()
                self.fs.links[key] = original
        for scope, field, wrong in (
            ("session", "ifacename", "custom"),
            ("session", "tpgt", "4294967295"),
            ("host", "proc_name", "qla4xxx"),
        ):
            path = os.path.join(paths[scope], field)
            with open(path) as handle:
                original = handle.read().strip()
            self.fs.write(path, wrong)
            with self.subTest(field=field):
                with self.assertRaises(connect.ZonalAffinityError):
                    self.read()
            self.fs.write(path, original)

    def test_extra_or_orphan_connections_refuse(self):
        self.fs.add(100)
        for name in ("connection100:1", "connection999:0"):
            path = os.path.join(self.fs.root, "iscsi_connection", name)
            os.makedirs(self.fs.actual(path))
            with self.subTest(connection=name):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "connection"):
                    self.read()
            os.rmdir(self.fs.actual(path))

    def test_readiness_requires_all_kernel_health_and_negotiated_digests(self):
        paths = self.fs.add(100)
        for scope, field, bad in (
            ("session", "state", "FAILED"), ("connection", "state", "down"),
            ("host", "state", "offline"), ("connection", "header_digest", "0"),
            ("connection", "data_digest", "0"), ("lun", "state", "offline"),
        ):
            path = os.path.join(paths[scope], field)
            with open(path) as handle:
                original = handle.read().strip()
            self.fs.write(path, bad)
            with self.subTest(field=field, scope=scope):
                self.assertFalse(self.read()[0]["healthy"])
            self.fs.write(path, original)

    def test_pending_disk_scan_is_not_ready_then_becomes_ready(self):
        self.fs.add(100, disk=False)
        self.assertFalse(self.read(pending=True)[0]["healthy"])
        self.fs.add_disk(100)
        self.assertTrue(self.read(pending=True)[0]["healthy"])

    def test_changed_snapshot_is_not_accepted(self):
        paths = self.fs.add(100)
        snapshot = connect._snapshot_zonal_sessions
        calls = []

        def changing(*args, **kwargs):
            if calls:
                self.fs.write(os.path.join(paths["connection"], "persistent_address"), VIPS[1])
            calls.append(True)
            return snapshot(*args, **kwargs)

        with mock.patch.object(connect, "_snapshot_zonal_sessions", side_effect=changing):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "changed"):
                self.read()

    def test_pending_read_requires_repeated_healthy_evidence(self):
        paths = self.fs.add(100)
        snapshot = connect._snapshot_zonal_sessions
        calls = []

        def changing(*args, **kwargs):
            if calls:
                self.fs.write(os.path.join(paths["connection"], "state"), "down")
            calls.append(True)
            return snapshot(*args, **kwargs)

        with mock.patch.object(connect, "_snapshot_zonal_sessions", side_effect=changing):
            self.assertFalse(self.read(pending=True)[0]["healthy"])

    def test_empty_missing_and_multiline_attributes_are_not_defaults(self):
        paths = self.fs.add(100)
        path = os.path.join(paths["session"], "ifacename")
        for value in ("", "(null)", "default\ncustom", " "):
            with self.subTest(value=value):
                self.fs.write(path, value)
                with self.assertRaises(connect.ZonalAffinityError):
                    self.read()


class NativeIscsi:
    """Argv-level native/daemon model; real subprocesses and initiators cannot escape."""

    def __init__(self):
        self.nodes, self.sessions = [], {}
        self.commands, self.mutations, self.login_hosts = [], [], []
        self.fail_at = None
        self.suppress_login = False
        self.pending_reads = 0
        self.remaining_pending = {}
        self.unsigned_tpgt = True
        self.inventory_warning = ""
        self.snapshot_hook = None
        self.snapshots = 0
        self.mutation_hook = None
        self.show_hook = None

    def add_session(self, iqn, host, port=3260):
        sid = str(101 + 37 * len(self.sessions))
        self.sessions[sid] = dict(
            sid=sid, iqn=iqn, portal=(host, port, 7), current_portal=("192.168.50.50", 4420),
            healthy=True, identity=("session-" + sid, "host-" + sid, "connection-" + sid),
            states=("LOGGED_IN", "up", "running", "1", "1"),
            disks=(("lun-" + sid, "disk-" + sid, "running"),),
        )
        self.remaining_pending[sid] = self.pending_reads
        return sid

    def seed(self, plan):
        self.nodes.extend(n["fields"] for n in make_nodes(plan))
        for host in plan["slots"]:
            self.add_session(plan["iqn"], host, plan["port"])

    def snapshot(self, plans, allow_pending=False):
        self.snapshots += 1
        if self.snapshot_hook:
            self.snapshot_hook(self, self.snapshots)
        entries = []
        for sid, session in sorted(self.sessions.items()):
            if not any(connect._selected_zonal_entries(p, [session]) for p in plans):
                continue
            entry = copy.deepcopy(session)
            if self.remaining_pending.get(sid, 0):
                self.remaining_pending[sid] -= 1
                entry["healthy"] = False
            entries.append(entry)
        return entries

    def __call__(self, command, **kwargs):
        assert command[:3] == ["sudo", "-n", "iscsiadm"], command
        assert kwargs["env"]["LC_ALL"] == "C"
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not kwargs.get("shell")
        self.commands.append(command)
        args = command[3:]

        def result(out="", err="", code=0):
            process = completed_process(out, err, code)(command, **kwargs)
            process.poll.return_value = code
            process.wait.return_value = code
            return process

        if args == ["-m", "node"]:
            if not self.nodes:
                return result(err="iscsiadm: No records found" + self.inventory_warning, code=21)
            return result("\n".join(
                "{},{}\t  {}".format(
                    "{}:{}".format(
                        n["node.conn[0].address"] if "." in n["node.conn[0].address"]
                        else "[" + n["node.conn[0].address"] + "]",
                        n["node.conn[0].port"],
                    ),
                    "4294967295" if self.unsigned_tpgt and n["node.tpgt"] == "-1" else n["node.tpgt"],
                    n["node.name"],
                ) for n in self.nodes
            ), err=self.inventory_warning)
        matches = []
        if args[:2] == ["-m", "node"]:
            iqn = args[args.index("--targetname") + 1]
            portal = args[args.index("--portal") + 1]
            host, port, _ = connect._parse_state_portal(portal if "," in portal else portal + ",-1")
            matches = [n for n in self.nodes if n["node.name"] == iqn
                       and n["node.conn[0].address"] == host and n["node.conn[0].port"] == str(port)]
            if args[-2:] == ["--op", "show"]:
                assert "--interface" not in args
                if self.show_hook:
                    self.show_hook(self, iqn, host)
                return result("\n".join(
                    "# BEGIN RECORD\n" + "\n".join(" \t{} =\t{} ".format(k, v) for k, v in n.items())
                    + "\n# END RECORD" for n in matches
                ))
        self.mutations.append(command)
        if len(self.mutations) == self.fail_at:
            return result(err="native request rejected", code=15)
        if args[:2] == ["-m", "session"]:
            assert args[-2:] == ["--op", "new"], "No native session inventory is allowed"
            seed = self.sessions[args[3]]
            self.login_hosts.append(seed["portal"][0])
            if not self.suppress_login:
                self.add_session(seed["iqn"], *seed["portal"][:2])
        elif args[-2:] == ["--op", "new"]:
            assert not matches, "Never replace an existing node"
            node = make_nodes(dict(iqn=iqn, vips=[host], port=port, slots=[host]))[0]["fields"]
            node.update({
                "node.startup": "automatic", "node.conn[0].startup": "automatic",
                "node.session.nr_sessions": "8",
                "node.conn[0].iscsi.HeaderDigest": "None", "node.conn[0].iscsi.DataDigest": "None",
            })
            self.nodes.append(node)
        elif "--login" in args:
            assert len(matches) == 1
            self.login_hosts.append(host)
            if not self.suppress_login:
                for _ in range(int(matches[0]["node.session.nr_sessions"])):
                    self.add_session(iqn, host, port)
        else:
            assert "--op" in args and args[args.index("--op") + 1] == "update", args
            assert len(matches) == 1
            matches[0][args[args.index("-n") + 1]] = args[args.index("-v") + 1]
        if self.mutation_hook:
            self.mutation_hook(self, command)
        return result()


class ZonalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeIscsi()
        patches = {
            "resolve_zonal_affinity_context": mock.Mock(return_value=("pinned-sub", "eastus-az3")),
            "get_mapped_volume_target": mock.Mock(return_value=(RAW_IQN, "portal.example", 3260)),
            "_lookup_target_addresses": mock.Mock(return_value=VIPS),
            "_snapshot_zonal_sessions": mock.Mock(side_effect=lambda *a, **kw: self.native.snapshot(*a, **kw)),
            "check_connection": mock.Mock(side_effect=AssertionError("legacy inventory")),
            "connect_volume": mock.Mock(side_effect=AssertionError("legacy mutation")),
        }
        for name, replacement in patches.items():
            patcher = mock.patch.object(connect, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mapping, self.discovery, self.lookup = (
            patches["resolve_zonal_affinity_context"], patches["get_mapped_volume_target"],
            patches["_lookup_target_addresses"],
        )
        patcher = mock.patch.object(connect.subprocess, "Popen", side_effect=lambda *a, **kw: self.native(*a, **kw))
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(connect.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("builtins.print")
        self.print_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def run_connect(self, volumes=None):
        connect.connect_volumes(None, "rg", "san", "vg", volumes or ["volume1"], 32, True)

    def test_actual_commands_allocate_numeric_vips_full_counts_crc_and_sid_clones(self):
        for case in ADDRESS_CASES:
            if "expected" not in case:
                continue
            with self.subTest(case=case["name"]):
                self.native = NativeIscsi()
                self.lookup.return_value = case["addresses"]
                self.run_connect()
                plan = make_plan(case["addresses"])
                self.assertEqual(plan["slots"], self.native.login_hosts)
                self.assertEqual(32, len(self.native.sessions))
                self.assertEqual(["11", "11", "10"], [n["node.session.nr_sessions"] for n in self.native.nodes])
                self.assertTrue(all(n["node.startup"] == "automatic" and
                                    n["node.conn[0].startup"] == "manual" for n in self.native.nodes))
                self.assertEqual(3, sum("--login" in c for c in self.native.mutations))
                clones = [c for c in self.native.commands if c[3:5] == ["-m", "session"]]
                self.assertEqual(29, len(clones))
                self.assertTrue(all(c[-2:] == ["--op", "new"] for c in clones))
                self.assertEqual(["101", "138", "175"], [c[6] for c in clones[:3]])
                self.assertNotIn("portal.example", str(self.native.commands))
                self.assertNotIn("-P", str(self.native.commands))
                for command in self.native.mutations:
                    if command[3:5] == ["-m", "node"]:
                        self.assertEqual(IQN, command[command.index("--targetname") + 1])
                        self.assertEqual("default", command[command.index("--interface") + 1])
                for key in ("HeaderDigest", "DataDigest"):
                    changes = [c for c in self.native.mutations if "node.conn[0].iscsi." + key in c]
                    self.assertEqual(3, len(changes))
                    self.assertTrue(all(c[-1] == "CRC32C" for c in changes))

    def test_complete_matching_state_skips_with_zero_mutation(self):
        self.native.seed(make_plan())
        self.run_connect()
        self.assertEqual([], self.native.mutations)
        self.assertTrue(any("Skipped; verified" in str(c) for c in self.print_mock.call_args_list))
        self.assertTrue(all(c[3:5] == ["-m", "node"] for c in self.native.commands))

    def test_returned_nondefault_port_used_everywhere(self):
        self.discovery.return_value = (RAW_IQN, "portal.example", 4420)
        self.run_connect()
        self.assertTrue(all(n["node.conn[0].port"] == "4420" for n in self.native.nodes))
        self.assertTrue(all(s["portal"][1] == 4420 for s in self.native.sessions.values()))

    def test_second_volume_dns_failure_prevents_even_inventory(self):
        self.discovery.side_effect = [(RAW_IQN, "one.example", 3260), (RAW_IQN + "2", "two.example", 3260)]
        self.lookup.side_effect = [VIPS] + [OSError("DNS unavailable")] * 3
        with self.assertRaisesRegex(connect.ZonalAffinityError, "two.example.*3 attempts"):
            self.run_connect(["one", "two"])
        self.popen.assert_not_called()
        self.assertEqual(0, self.native.snapshots)

    def test_second_volume_state_conflict_prevents_first_volume_mutation(self):
        other = RAW_IQN + "2"
        self.discovery.side_effect = [(RAW_IQN, "portal.example", 3260), (other, "portal.example", 3260)]
        self.native.add_session(other, "portal.example")
        with self.assertRaisesRegex(connect.ZonalAffinityError, "conflicting state"):
            self.run_connect(["one", "two"])
        self.assertEqual([], self.native.mutations)
        self.lookup.assert_called_once_with("portal.example")

    def test_later_volume_persistent_settings_checked_before_any_mutation(self):
        other = RAW_IQN + "2"
        self.discovery.side_effect = [(RAW_IQN, "portal.example", 3260), (other, "portal.example", 3260)]
        self.native.seed(make_plan(raw_iqn=other))
        self.native.nodes[-1]["node.session.nr_sessions"] = "9"
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
            self.run_connect(["one", "two"])
        self.assertEqual([], self.native.mutations)

    def test_earlier_volume_identity_change_during_later_volume_proof_refuses(self):
        other = RAW_IQN + "2"
        self.discovery.side_effect = [(RAW_IQN, "portal.example", 3260), (other, "portal.example", 3260)]
        self.native.seed(make_plan())
        self.native.seed(make_plan(raw_iqn=other))
        changed = []

        def show(native, iqn, host):
            if iqn == other + ":az-eastus-az3" and not changed:
                next(iter(native.sessions.values()))["portal"] = (VIPS[1], 3260, 7)
                changed.append(True)

        self.native.show_hook = show
        with self.assertRaisesRegex(connect.ZonalAffinityError, "changed"):
            self.run_connect(["one", "two"])
        self.assertEqual([], self.native.mutations)

    def test_unexpected_inventory_stderr_is_not_partial_proof(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.native = NativeIscsi()
                if existing:
                    self.native.seed(make_plan())
                self.native.inventory_warning = "\nCannot stat path: access denied"
                with self.assertRaisesRegex(connect.ZonalAffinityError, "Cannot stat"):
                    self.run_connect()
                self.assertEqual([], self.native.mutations)

    def test_missing_seed_setting_despite_success_never_logs_in(self):
        def drop_setting(native, command):
            if "node.conn[0].startup" in command:
                native.nodes[-1]["node.conn[0].startup"] = "automatic"

        self.native.mutation_hook = drop_setting
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
            self.run_connect()
        self.assertEqual([], self.native.login_hosts)

    def test_pending_first_session_must_be_ready_before_cloning(self):
        self.native.pending_reads = 1
        self.run_connect()
        self.assertEqual(32, self.sleep.call_count)
        self.assertEqual(32, len(self.native.sessions))

    def test_successful_native_request_without_session_times_out(self):
        self.native.suppress_login = True
        with self.assertRaisesRegex(connect.ZonalAffinityError, "Timed out.*kernel-ready"):
            self.run_connect()
        self.assertEqual(1, len(self.native.login_hosts))
        self.assertEqual(connect.ISCSI_READY_ATTEMPTS - 1, self.sleep.call_count)
        self.assertTrue(all(n["node.startup"] == "manual" and
                            n["node.conn[0].startup"] == "manual" for n in self.native.nodes))

    def test_extra_session_is_not_selected_as_seed(self):
        def extra(native, command):
            if "--login" in command:
                native.add_session(IQN, VIPS[0])
        self.native.mutation_hook = extra
        with self.assertRaisesRegex(connect.ZonalAffinityError, "changed unexpectedly"):
            self.run_connect()
        self.assertEqual(1, len(self.native.login_hosts))

    def test_full_persistence_is_verified_after_successful_updates(self):
        def drop_persistence(native, command):
            if "node.session.nr_sessions" in command and command[-1] == "11":
                native.nodes[0]["node.session.nr_sessions"] = "10"
        self.native.mutation_hook = drop_persistence
        with self.assertRaisesRegex(connect.ZonalAffinityError, "persistent node"):
            self.run_connect()
        self.assertEqual(32, len(self.native.sessions))
        self.assertFalse(any("Verified 32" in str(c) for c in self.print_mock.call_args_list))

    def test_native_partial_failure_refuses_retry_without_cleanup(self):
        self.native.fail_at = 24
        with self.assertRaisesRegex(connect.ZonalAffinityError, "exit 15.*native request rejected"):
            self.run_connect()
        self.assertGreater(len(self.native.sessions), 0)
        self.assertEqual(24, len(self.native.mutations))
        self.native.fail_at = None
        with self.assertRaisesRegex(connect.ZonalAffinityError, "No automatic rollback"):
            self.run_connect()
        self.assertEqual(24, len(self.native.mutations))
        self.assertFalse(any("--logout" in c or "delete" in c for c in self.native.commands))

    def test_later_apply_failure_identifies_earlier_verified_volume(self):
        self.discovery.side_effect = [(RAW_IQN, "portal.example", 3260), (RAW_IQN + "2", "portal.example", 3260)]
        self.native.fail_at = 57  # 56 mutations complete the first volume.
        with self.assertRaisesRegex(connect.ZonalAffinityError, "Stopped at volume 'two'; earlier verified volumes: one"):
            self.run_connect(["one", "two"])
        self.assertEqual(32, len(self.native.sessions))

    def test_unrelated_state_is_not_used_as_seed_or_modified(self):
        unrelated = "iqn.unrelated"
        old_sid = self.native.add_session(unrelated, "127.0.0.1")
        node = make_nodes(make_plan(raw_iqn=unrelated))[0]["fields"]
        node["node.conn[0].address"] = "169.254.10.1"
        self.native.nodes.append(node)
        old_node = dict(node)
        self.run_connect()
        self.assertEqual(old_node, self.native.nodes[0])
        self.assertEqual(33, len(self.native.sessions))
        self.assertTrue(all(unrelated not in c for c in self.native.mutations))
        self.assertTrue(all(old_sid not in c for c in self.native.mutations))

    def test_native_unrelated_ipv6_forms_allow_both_connect_and_skip(self):
        for host in ("fe80::1%eth0", "fe80::1%eth0.42", "::ffff:10.0.0.1"):
            with self.subTest(host=host):
                self.native = NativeIscsi()
                node = make_nodes(make_plan(raw_iqn="iqn.unrelated"))[0]["fields"]
                node["node.conn[0].address"] = host
                self.native.nodes.append(node)
                self.run_connect()
                mutations = list(self.native.mutations)
                self.run_connect()
                self.assertEqual(mutations, self.native.mutations)
                self.assertEqual(node, self.native.nodes[0])
                self.assertEqual(32, len(self.native.sessions))

    def test_enabled_entrypoint_rejects_non32_before_legacy_clamp(self):
        args = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "one", "--enable-zonal-affinity"]
        for count in ("0", "-1", "4", "31", "33", "64", "invalid"):
            with self.subTest(count=count):
                with mock.patch.object(connect, "check_iscsi"), mock.patch.object(connect, "check_mpio"):
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "exactly 32"):
                        connect.main(args + ["-s", count])
        self.mapping.assert_not_called()
        self.popen.assert_not_called()

    def test_actual_imds_cli_dns_native_flow_uses_pinned_subscription(self):
        subscription = "00000000-0000-0000-0000-000000000001"
        responses = [subscription, "eastus", locations_payload(), json.dumps({
            "targetIqn": RAW_IQN, "targetPortalHostname": "portal.example", "targetPortalPort": 3260,
        })]
        original_mapping = self.mapping
        original_discovery = self.discovery
        # The module's original functions are saved below before setUp patches.
        with mock.patch.object(connect, "resolve_zonal_affinity_context", REAL_CONTEXT):
            with mock.patch.object(connect, "get_mapped_volume_target", REAL_TARGET):
                with mock.patch.object(connect, "get_vm_compute_metadata", return_value={
                    "subscriptionId": subscription, "zone": "2", "location": "eastus",
                }):
                    def launch(command, **kwargs):
                        if command[0] == "az":
                            return completed_process(responses.pop(0))(command, **kwargs)
                        return self.native(command, **kwargs)
                    self.popen.side_effect = launch
                    self.run_connect()
        self.assertEqual([], responses)
        volume_call = next(c.args[0] for c in self.popen.call_args_list if c.args[0][:3] == ["az", "elastic-san", "volume"])
        self.assertEqual(subscription, volume_call[volume_call.index("--subscription") + 1])
        self.assertEqual(32, len(self.native.sessions))
        original_mapping.assert_not_called()
        original_discovery.assert_not_called()


REAL_CONTEXT = connect.resolve_zonal_affinity_context
REAL_TARGET = connect.get_mapped_volume_target


class NativeRunnerTests(unittest.TestCase):
    def test_exact_empty_inventory_is_distinct_from_errors_and_partial_output(self):
        for code, out, err, accepted in (
            (21, "", "iscsiadm: No records found", True),
            (21, "No records found", "", True),
            (21, "", "permission denied", False),
            (21, "10.0.0.1:3260,-1 iqn.test", "No records found", False),
            (0, "", "", False), (0, "records", "Cannot stat path", False),
            (1, "", "No records found", False),
        ):
            with self.subTest(code=code, out=out, err=err):
                launch = completed_process(out, err, code)

                def start(command, **kwargs):
                    process = launch(command, **kwargs)
                    process.poll.return_value = code
                    return process

                with mock.patch.object(connect.subprocess, "Popen", side_effect=start):
                    if accepted:
                        self.assertEqual("", connect._run_zonal_iscsiadm(["-m", "node"], "No records found"))
                    else:
                        with self.assertRaisesRegex(connect.ZonalAffinityError, "No automatic rollback"):
                            connect._run_zonal_iscsiadm(["-m", "node"], "No records found")

    def test_timeout_and_interruption_kill_and_reap_only_direct_client(self):
        for failure in (subprocess.TimeoutExpired("iscsiadm", 30), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                process = mock.Mock(pid=123)
                process.wait.side_effect = [failure, -9]
                process.poll.return_value = None
                expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else connect.ZonalAffinityError
                with mock.patch.object(connect.subprocess, "Popen", return_value=process) as launch:
                    with mock.patch.object(connect.os, "killpg", create=True) as killpg:
                        with self.assertRaises(expected):
                            connect._run_zonal_iscsiadm(["-m", "node"])
                process.kill.assert_called_once_with()
                self.assertEqual([mock.call(timeout=30), mock.call(timeout=1)], process.wait.call_args_list)
                killpg.assert_not_called()
                self.assertTrue(launch.call_args.kwargs["stdout"].closed)
                self.assertTrue(launch.call_args.kwargs["stderr"].closed)

    def test_cleanup_failure_is_explicit_and_bounded(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("iscsiadm", 30)
        with mock.patch.object(connect.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "could not be reaped"):
                connect._run_zonal_iscsiadm(["-m", "node"])
        self.assertEqual([mock.call(timeout=30), mock.call(timeout=1)], process.wait.call_args_list)
        process.kill.assert_called_once_with()

    def test_start_failure_and_oversized_output_are_explicit(self):
        with mock.patch.object(connect.subprocess, "Popen", side_effect=OSError("not installed")):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "failed to start.*not installed"):
                connect._run_zonal_iscsiadm(["-m", "node"])
        launch = completed_process("x" * (connect.ISCSI_OUTPUT_LIMIT_BYTES + 1))

        def start(command, **kwargs):
            process = launch(command, **kwargs)
            process.poll.return_value = 0
            return process

        with mock.patch.object(connect.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "output exceeded"):
                connect._run_zonal_iscsiadm(["-m", "node"])

    def test_real_timeout_reaps_client_without_harming_inherited_output_holder(self):
        real_popen = subprocess.Popen
        children = []
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "holder-response")

            def start(command, **kwargs):
                self.assertEqual(["sudo", "-n", "iscsiadm", "-m", "node"], command)
                holder = real_popen([
                    sys.executable, "-c",
                    "import sys; from pathlib import Path; sys.stdin.readline(); "
                    "Path(sys.argv[1]).write_text('still-alive')", marker,
                ], stdin=subprocess.PIPE, stdout=kwargs["stdout"], stderr=kwargs["stderr"])
                children.append(holder)
                client = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
                children.append(client)
                return client

            started = time.monotonic()
            try:
                with mock.patch.object(connect.subprocess, "Popen", side_effect=start):
                    with mock.patch.object(connect, "ISCSI_COMMAND_TIMEOUT_SECONDS", 0.5):
                        with self.assertRaisesRegex(connect.ZonalAffinityError, "timed out"):
                            connect._run_zonal_iscsiadm(["-m", "node"])
                self.assertLess(time.monotonic() - started, 5)
                self.assertIsNotNone(children[1].returncode)
                self.assertIsNone(children[0].poll())
                children[0].stdin.write(b"respond after timeout\n")
                children[0].stdin.flush()
                children[0].stdin.close()
                children[0].wait(timeout=5)
                with open(marker) as response:
                    self.assertEqual("still-alive", response.read())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                    if child.stdin and not child.stdin.closed:
                        child.stdin.close()


if __name__ == "__main__":
    unittest.main()
