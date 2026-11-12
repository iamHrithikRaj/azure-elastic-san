import importlib.util
import io
import json
import os
import subprocess
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "disconnect_for_documentation.py")
SPEC = importlib.util.spec_from_file_location("disconnect_for_documentation", SCRIPT_PATH)
disconnect = importlib.util.module_from_spec(SPEC)
with mock.patch("os.path.exists", side_effect=lambda path: path == "/usr/bin/apt-get"):
    SPEC.loader.exec_module(disconnect)


IQN = "iqn.2024-01.com.microsoft:volume"
ZONAL_IQN = IQN + ":az-eastus-az3"


def completed_process(stdout="", stderr="", returncode=0):
    process = mock.Mock()
    process.communicate.return_value = (stdout.encode("utf-8"), stderr.encode("utf-8"))
    process.returncode = returncode
    return process


class DisconnectTests(unittest.TestCase):
    def lookup(self, result=None, error=None):
        payload = json.dumps({
            "targetIqn": IQN, "targetPortalHostname": "portal.example", "targetPortalPort": 3260,
        }).encode("utf-8")
        result = result or subprocess.CompletedProcess([], 0, payload, b"")
        process = completed_process(result.stdout.decode("utf-8"), result.stderr.decode("utf-8"), result.returncode)
        process.poll.return_value = result.returncode
        with mock.patch.object(disconnect, "check_azcli"), mock.patch.object(
            disconnect.subprocess, "Popen", return_value=process,
        ):
            with mock.patch.object(disconnect.subprocess, "run", return_value=result, side_effect=error) as run:
                value = disconnect.get_iqns("subscription with spaces", "rg", "san", "vg", "volume")
        return value, run

    def test_lookup_preserves_subscription_boundary_and_bounds_execution(self):
        value, run = self.lookup()
        self.assertEqual((IQN, "portal.example", 3260), value)
        run.assert_called_once_with(
            ["az", "elastic-san", "volume", "show", "-g", "rg", "-e", "san", "-v", "vg",
             "-n", "volume", "--query", "storageTarget", "--subscription", "subscription with spaces"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )

    def test_lookup_nonzero_exit_without_english_error_fails(self):
        with self.assertRaisesRegex(Exception, "denied"):
            self.lookup(subprocess.CompletedProcess([], 4, b"{}", b"denied"))

    def test_lookup_successful_stderr_is_diagnostic(self):
        payload = json.dumps({
            "targetIqn": IQN, "targetPortalHostname": "portal.example", "targetPortalPort": 3260,
        }).encode("utf-8")
        value, _ = self.lookup(subprocess.CompletedProcess([], 0, payload, b"error diagnostic only"))
        self.assertEqual((IQN, "portal.example", 3260), value)

    def test_lookup_timeout_kills_and_reaps_cli(self):
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.__exit__.side_effect = lambda *args: (process.wait(), False)[1]
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["az"], 10), (b"", b""),
        ]
        process.poll.return_value = None
        with mock.patch.object(disconnect, "check_azcli"), mock.patch.object(disconnect.time, "sleep"), mock.patch.object(
            disconnect.subprocess, "Popen", return_value=process,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                disconnect.get_iqns(None, "rg", "san", "vg", "volume")
        process.kill.assert_called_once_with()
        process.wait.assert_called()

    def test_import_needs_no_package_manager(self):
        module = importlib.util.module_from_spec(SPEC)
        with mock.patch("os.path.exists", return_value=False), mock.patch("subprocess.Popen") as run:
            SPEC.loader.exec_module(module)
        run.assert_not_called()

    def test_azure_cli_on_path_needs_no_package_record(self):
        with mock.patch("shutil.which", return_value="/opt/azure/bin/az") as which:
            with mock.patch.object(disconnect.subprocess, "Popen") as run:
                disconnect.check_azcli()
        which.assert_called_once_with("az")
        run.assert_not_called()

    def test_case_insensitive_identity_preserves_original_command_target(self):
        target = ZONAL_IQN.upper()
        commands, _ = self.run_disconnect(
            "10.0.0.1:3260,-1 " + target,
            "tcp: [1] 10.0.0.1:3260,-1 " + target,
        )
        self.assert_target_cleanup(commands, [target])

    def run_disconnect(self, nodes="", sessions="", results=None):
        processes = [
            nodes if not isinstance(nodes, str) else completed_process(nodes),
            sessions if not isinstance(sessions, str) else completed_process(sessions),
        ]
        processes.extend(results or [])

        def popen(command, **kwargs):
            return processes.pop(0) if processes else completed_process()

        with mock.patch.object(disconnect.subprocess, "Popen", side_effect=popen) as run:
            with mock.patch.object(disconnect.sys, "stdout", new_callable=io.StringIO) as output:
                disconnect.disconnect_volume("volume", IQN, "portal.example", 3260)
        commands = []
        for call in run.call_args_list:
            command = call.args[0]
            if command[:2] == ["sudo", "-n"]:
                command = ["sudo"] + command[2:]
            elif command[:1] == ["iscsiadm"]:
                command = ["sudo"] + command
            commands.append(command)
        return commands, output.getvalue()

    def assert_target_cleanup(self, commands, targets):
        expected = [
            ["sudo", "iscsiadm", "-m", "node"],
            ["sudo", "iscsiadm", "-m", "session"],
        ]
        for target in sorted(targets):
            expected.extend([
                ["sudo", "iscsiadm", "-m", "node", "-T", target, "-u"],
                ["sudo", "iscsiadm", "-m", "node", "-T", target, "-o", "delete"],
            ])
        self.assertEqual(expected, commands)

    def test_plain_iqn_and_fqdn_are_logged_out_and_deleted(self):
        commands, _ = self.run_disconnect(
            "portal.example:3260,-1 " + IQN + "\n",
            "tcp: [1] portal.example:3260,-1 " + IQN + " (non-flash)\n",
        )
        self.assert_target_cleanup(commands, [IQN])

    def test_decorated_iqn_needs_no_zone_parameter(self):
        commands, _ = self.run_disconnect(
            "portal.example:3260,-1 " + ZONAL_IQN + "\n",
            "tcp: [1] portal.example:3260,-1 " + ZONAL_IQN + " (non-flash)\n",
        )
        self.assert_target_cleanup(commands, [ZONAL_IQN])

    def test_multiple_ip_portals_deduplicate_target_wide_cleanup(self):
        portals = ["10.0.0.1:3260,-1", "10.0.0.2:3260,4294967295", "[fd00::1]:3261,1"]
        nodes = "".join("  {}\t{}\n".format(portal, ZONAL_IQN) for portal in portals)
        sessions = "".join(
            " tcp:  [ {} ]\t{}\t{} (non-flash)\n".format(index, portal, ZONAL_IQN)
            for index, portal in enumerate(portals, 1)
        )
        commands, _ = self.run_disconnect(nodes, sessions)
        self.assert_target_cleanup(commands, [ZONAL_IQN])
        self.assertFalse(any("--portal" in command or "-p" in command for command in commands))

    def test_similar_prefix_targets_are_not_selected(self):
        unrelated = [IQN + "2", IQN + ":azother", IQN + ":other", "prefix-" + ZONAL_IQN]
        nodes = "".join("10.0.0.1:3260,-1 {}\n".format(target) for target in unrelated)
        sessions = "".join(
            "tcp: [{}] 10.0.0.1:3260,-1 {} (non-flash)\n".format(index, target)
            for index, target in enumerate(unrelated, 1)
        )
        commands, output = self.run_disconnect(nodes, sessions)
        self.assert_target_cleanup(commands, [])
        self.assertIn("volume [{}]: Skipped as this volume is not connected".format(IQN), output)

    def test_union_includes_plain_and_all_zonal_targets_but_not_neighbors(self):
        other_zone = IQN + ":az-eastus-az1"
        commands, _ = self.run_disconnect(
            "portal.example:3260,-1 {}\n10.0.0.1:3260,1 {}\n10.0.0.2:3260,1 {}2\n".format(
                IQN, ZONAL_IQN, IQN
            ),
            "tcp: [1] 10.0.0.3:3260,1 {} (non-flash)\n".format(other_zone),
        )
        self.assert_target_cleanup(commands, [IQN, ZONAL_IQN, other_zone])

    def test_persistent_records_without_sessions_are_deleted(self):
        no_sessions = completed_process(stderr="iscsiadm: No active sessions.", returncode=21)
        commands, output = self.run_disconnect(
            "10.0.0.1:3260,-1 " + ZONAL_IQN,
            no_sessions,
            results=[no_sessions, completed_process()],
        )
        self.assert_target_cleanup(commands, [ZONAL_IQN])
        self.assertNotIn("not connected", output)

    def test_live_session_without_node_inventory_is_selected(self):
        no_records = completed_process(stderr="iscsiadm: No records found", returncode=21)
        commands, output = self.run_disconnect(
            no_records,
            "tcp: [7] 10.0.0.1:3260,-1 " + ZONAL_IQN,
            results=[completed_process(), no_records],
        )
        self.assert_target_cleanup(commands, [ZONAL_IQN])
        self.assertNotIn("not connected", output)

    def test_empty_inventories_are_skipped_gracefully(self):
        for results in (
            ("", ""),
            (
                completed_process(stderr="iscsiadm: No records found", returncode=21),
                completed_process(stderr="iscsiadm: No active sessions.", returncode=21),
            ),
        ):
            with self.subTest(results=results):
                commands, output = self.run_disconnect(*results)
                self.assert_target_cleanup(commands, [])
                self.assertIn("not connected", output)

    def test_inventory_failure_prevents_mutation_even_if_nodes_match(self):
        processes = [
            completed_process("10.0.0.1:3260,-1 " + ZONAL_IQN),
            completed_process(stderr="permission denied", returncode=13),
        ]
        with mock.patch.object(disconnect.subprocess, "Popen", side_effect=processes) as run:
            with self.assertRaisesRegex(Exception, "permission denied"):
                disconnect.disconnect_volume("volume", IQN, "portal.example", 3260)
        self.assertEqual(2, run.call_count)

    def test_logout_failure_prevents_node_deletion(self):
        processes = [
            completed_process("10.0.0.1:3260,-1 " + ZONAL_IQN),
            completed_process("tcp: [1] 10.0.0.1:3260,-1 " + ZONAL_IQN),
            completed_process(stderr="logout failed", returncode=10),
        ]
        with mock.patch.object(disconnect.subprocess, "Popen", side_effect=processes) as run:
            with self.assertRaisesRegex(Exception, "logout failed"):
                disconnect.disconnect_volume("volume", IQN, "portal.example", 3260)
        self.assertEqual(3, run.call_count)
        self.assertEqual("-u", run.call_args.args[0][-1])

    def test_delete_failure_is_not_reported_as_success(self):
        with self.assertRaisesRegex(Exception, "exit code 6"):
            self.run_disconnect(
                "10.0.0.1:3260,-1 " + IQN,
                "",
                results=[completed_process(), completed_process(returncode=6)],
            )

    def test_unrecognized_inventory_is_not_misreported_as_disconnected(self):
        with self.assertRaisesRegex(Exception, "Cannot parse"):
            self.run_disconnect("unexpected node output")


if __name__ == "__main__":
    unittest.main()
