import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_connect_for_documentation import connect, RecordingFcntl


class PartialConnectionTests(unittest.TestCase):
    def run_plan(self, addresses, states, reject_at=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        path = Path(stack.enter_context(tempfile.TemporaryDirectory()), "iscsi.conf")
        original = b"# operator-owned bytes\n"
        path.write_bytes(original)
        plan = connect.VolumeConnectionPlan(
            "volume", "iqn.test:volume", addresses[0],
            ["esan-volume-s{}".format(i + 1) for i in range(len(addresses))], addresses,
        )
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
        stack.enter_context(mock.patch.object(connect, "list_iscsi_sessions", side_effect=lambda: list(live)))
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
        self.assertEqual([item["Target portal"] for item in live], ["192.0.2.1:3260"])
        self.assertEqual([entry.nickname for entry in connect.read_managed_entries(path.read_bytes())],
                         plan.nicknames[:1])
        self.assertIn(["iscsictl", "-R", "-n", plan.nicknames[1], "-c", str(path)], commands)

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
