import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_connect_for_documentation import connect, RecordingFcntl, verbose_session_block


class PartialConnectionTests(unittest.TestCase):
    def run_plan(self, addresses, states, reject_at=None, preexisting_entries=False, unrelated_portals=()):
        addresses = [address.removesuffix(":3260") for address in addresses]
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        path = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=".")), "iscsi.conf")
        original = b"# operator-owned bytes\n"
        plan = connect.VolumeConnectionPlan(
            "volume", "iqn.test:volume", addresses[0],
            ["esan-volume-s{}".format(i + 1) for i in range(len(addresses))], addresses,
        )
        path.write_bytes(original + (connect.render_full_managed_block([plan]) if preexisting_entries else b""))
        live = []
        commands = []

        def native(command, *_args):
            commands.append(command)
            if command[:3] == ["iscsictl", "-A", "-n"]:
                index = plan.nicknames.index(command[3])
                if index == reject_at:
                    raise connect.ElasticSanConnectError("Device busy")
                live.append({"Target name": plan.target_name, "Target portal": addresses[index],
                             "Session state": states[index]})
            elif command[:3] == ["iscsictl", "-R", "-n"]:
                address = addresses[plan.nicknames.index(command[3])]
                live[:] = [item for item in live if item["Target portal"] != address]
            else:
                raise AssertionError(command)

        stack.enter_context(mock.patch.object(connect, "fcntl", RecordingFcntl()))
        stack.enter_context(mock.patch.object(connect, "check_freebsd_mutation_prerequisites"))
        stack.enter_context(mock.patch.object(connect, "ensure_iscsid_enabled_and_running"))
        if hasattr(connect, "ensure_freebsd_prerequisites"):
            stack.enter_context(mock.patch.object(connect, "ensure_freebsd_prerequisites"))
        def inventory():
            unrelated = [{"Target name": "iqn.test:unrelated", "Target portal": portal, "Session state": "Connected"}
                         for portal in unrelated_portals]
            return connect._parse_iscsictl_verbose_sessions("".join(
                verbose_session_block(item["Target name"], item["Target portal"], item["Session state"])
                for item in live + unrelated))

        stack.enter_context(mock.patch.object(connect, "list_iscsi_sessions", side_effect=inventory))
        stack.enter_context(mock.patch.object(connect, "_run_command", side_effect=native))
        stack.enter_context(mock.patch.object(connect.time, "monotonic", side_effect=range(1000)))
        stack.enter_context(mock.patch.object(connect.time, "sleep"))
        output = io.StringIO()
        error = None
        with contextlib.redirect_stdout(output):
            try:
                connect.execute_connection_plan([plan], str(path), len(addresses))
            except connect.ElasticSanConnectError as raised:
                error = raised
        return plan, path, live, commands, output.getvalue(), error

    def test_unrelated_ipv6_inventory_does_not_block_connect(self):
        plan, path, live, commands, _output, error = self.run_plan(
            ["volume.example"], ["Connected"], unrelated_portals=["[2001:db8::1]:3260", "2001:db8::2"])
        self.assertIsNone(error)
        self.assertEqual(len(live), 1)
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())], plan.nicknames)
        self.assertEqual(commands, [["iscsictl", "-A", "-n", plan.nicknames[0], "-c", str(path)]])

    def test_native_portals_only_drop_an_unambiguous_default_port(self):
        portals = ["legacy.example:3260", "192.0.2.9:3260", "[2001:db8::1]:3260",
                   "2001:db8::2", "2001:db8::3:3260", "other:3261", "native:opaque:portal"]
        observed = connect._parse_iscsictl_verbose_sessions("".join(
            verbose_session_block("iqn.test:unrelated", portal) for portal in portals))
        self.assertEqual([item["Target portal"] for item in observed],
                         ["legacy.example", "192.0.2.9", "[2001:db8::1]",
                          "2001:db8::2", "2001:db8::3:3260", "other:3261", "native:opaque:portal"])

    def test_synchronous_capacity_rejection_keeps_connected_session_and_only_its_entry(self):
        plan, path, live, commands, output, _error = self.run_plan(
            ["host:3260"] * 32, ["Connected"] * 32, reject_at=1)
        self.assertEqual(len(live), 1)
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())],
                         plan.nicknames[:1])
        self.assertEqual(len([command for command in commands if command[1] == "-A"]), 2)
        self.assertIn("volume: 1 of 32 sessions connected.", output)
        self.assertIn("re-run with -s 1", output)

    def test_timeout_removes_only_unconnected_this_run_sessions_and_their_entries(self):
        plan, path, live, commands, _output, _error = self.run_plan(
            ["192.0.2.1:3260", "192.0.2.2:3260"], ["Connected", "Connecting"])
        self.assertEqual([item["Target portal"] for item in live], ["192.0.2.1"])
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())],
                         plan.nicknames[:1])
        self.assertIn(["iscsictl", "-R", "-n", plan.nicknames[1], "-c", str(path)], commands)

    def test_zero_connected_removes_this_runs_pending_sessions_and_restores_config(self):
        _plan, path, live, _commands, output, error = self.run_plan(
            ["host:3260"] * 2, ["Connecting"] * 2)
        self.assertEqual(live, [])
        self.assertEqual(path.read_bytes(), b"# operator-owned bytes\n")
        self.assertIsNotNone(error)
        self.assertIn("0 of 2 sessions connected.", output)
        self.assertIn("Inspect 'iscsictl -L -v'", output)
        self.assertNotIn("re-run with -s 0", output)

    def test_partial_connection_never_removes_entries_that_existed_before_the_run(self):
        plan, path, live, _commands, _output, _error = self.run_plan(
            ["host:3260"] * 2, ["Connected"] * 2, reject_at=1, preexisting_entries=True)
        self.assertEqual(len(live), 1)
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())],
                         plan.nicknames)

    def test_shared_portal_cleanup_fails_closed_instead_of_removing_connected_sessions(self):
        plan, path, live, commands, _output, error = self.run_plan(
            ["host:3260"] * 2, ["Connected", "Connecting"])
        self.assertEqual(len(live), 2)
        self.assertFalse(any(command[1] == "-R" for command in commands))
        self.assertIn("cannot safely remove", str(error))
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())],
                         plan.nicknames)


if __name__ == "__main__":
    unittest.main()
