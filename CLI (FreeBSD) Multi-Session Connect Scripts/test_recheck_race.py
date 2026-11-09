import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_connect_for_documentation import connect, RecordingFcntl


class RecheckRaceTests(unittest.TestCase):
    def test_dry_run_prints_the_exact_add_command_for_an_external_manager(self):
        plan = connect.VolumeConnectionPlan("volume", "iqn.test:volume", "host:3260", ["esan-volume-s1"])
        output = io.StringIO()
        with mock.patch.object(connect, "check_az_cli_available"), \
             mock.patch.object(connect, "build_connection_plan", return_value=[plan]), \
             mock.patch.object(connect, "execute_connection_plan", side_effect=AssertionError("mutation")), \
             contextlib.redirect_stdout(output):
            connect.main(["-g", "rg", "-e", "esan", "-v", "vg", "-n", "volume", "--dry-run",
                          "--config-path", "/etc/custom iscsi.conf"])
        self.assertIn("iscsictl -A -n esan-volume-s1 -c '/etc/custom iscsi.conf'", output.getvalue())

    def test_external_session_is_skipped_and_pre_volume_config_is_restored(self):
        plan = connect.VolumeConnectionPlan("volume", "iqn.test:volume", "host:3260", ["esan-volume-s1"])
        for original in [None, b"# operator\n", connect.render_full_managed_block([plan])]:
            with self.subTest(original=original), contextlib.ExitStack() as stack:
                path = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=".")), "iscsi.conf")
                if original is not None:
                    path.write_bytes(original)
                stack.enter_context(mock.patch.object(connect, "fcntl", RecordingFcntl()))
                stack.enter_context(mock.patch.object(connect, "ensure_freebsd_prerequisites"))
                stack.enter_context(mock.patch.object(connect, "list_iscsi_sessions", side_effect=[
                    [], [{"Target name": plan.target_name, "Target portal": "redirect:3260", "Session state": "Connected"}]]))
                add = stack.enter_context(mock.patch.object(connect, "add_iscsi_session"))
                connected = connect.execute_connection_plan([plan], str(path), 1)
                self.assertEqual(connected, [])
                self.assertEqual(path.read_bytes() if path.exists() else None, original)
                add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
