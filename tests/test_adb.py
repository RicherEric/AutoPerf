import subprocess
import unittest
from unittest.mock import MagicMock, patch

from autoperf.adb import AdbClient, AdbError
from autoperf.models import Device


def _completed(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class AdbClientTests(unittest.TestCase):
    def test_devices_parses_serial_state_and_metadata(self):
        output = (
            "List of devices attached\n"
            "1234ABCD       device usb:1-1 product:blueline model:Pixel_3 device:blueline transport_id:1\n"
            "\n"
        )
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout=output)):
            devices = AdbClient().devices()
        self.assertEqual(devices, [Device("1234ABCD", "device", "Pixel_3", "blueline", "1")])

    def test_devices_skips_blank_lines(self):
        output = "List of devices attached\n\n\n"
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout=output)):
            devices = AdbClient().devices()
        self.assertEqual(devices, [])

    def test_shell_rejects_invalid_serial_without_running_subprocess(self):
        with patch("autoperf.adb.subprocess.run") as run:
            with self.assertRaises(ValueError):
                AdbClient().shell("bad;serial", "ls")
            run.assert_not_called()

    def test_shell_returns_stdout_for_valid_serial(self):
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout="ok\n")) as run:
            output = AdbClient().shell("SERIAL_1", "ls", timeout=5)
        self.assertEqual(output, "ok\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["adb", "-s", "SERIAL_1", "shell", "ls"])
        self.assertEqual(kwargs["timeout"], 5)

    def test_run_wraps_missing_executable(self):
        with patch("autoperf.adb.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(AdbError):
                AdbClient().devices()

    def test_run_wraps_timeout(self):
        with patch("autoperf.adb.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=15)):
            with self.assertRaises(AdbError):
                AdbClient().devices()

    def test_run_raises_on_nonzero_exit_with_stderr_message(self):
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stderr="device offline\n", returncode=1)):
            with self.assertRaises(AdbError) as ctx:
                AdbClient().devices()
        self.assertIn("device offline", str(ctx.exception))

    def test_exec_out_args_builds_argv_without_running_anything(self):
        with patch("autoperf.adb.subprocess.run") as run:
            argv = AdbClient().exec_out_args("SERIAL_1", "screenrecord --output-format=h264 -")
        self.assertEqual(argv, ["adb", "-s", "SERIAL_1", "exec-out", "screenrecord --output-format=h264 -"])
        run.assert_not_called()

    def test_exec_out_args_rejects_invalid_serial(self):
        with self.assertRaises(ValueError):
            AdbClient().exec_out_args("bad;serial", "screencap -p")

    def test_connect_rejects_malformed_address_without_running_subprocess(self):
        with patch("autoperf.adb.subprocess.run") as run:
            with self.assertRaises(ValueError):
                AdbClient().connect("not-an-address")
            run.assert_not_called()

    def test_connect_succeeds_on_connected_to_output(self):
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout="connected to 192.168.1.50:5555\n")):
            result = AdbClient().connect("192.168.1.50:5555")
        self.assertIn("connected to", result)

    def test_connect_raises_when_adb_reports_failure_despite_exit_zero(self):
        # `adb connect` exits 0 even when it can't actually connect -- failure
        # must be detected from stdout content, not the return code.
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout="failed to connect to 1.2.3.4:5555\n")):
            with self.assertRaises(AdbError):
                AdbClient().connect("1.2.3.4:5555")

    def test_pair_rejects_malformed_address_or_code(self):
        with patch("autoperf.adb.subprocess.run") as run:
            with self.assertRaises(ValueError):
                AdbClient().pair("not-an-address", "123456")
            with self.assertRaises(ValueError):
                AdbClient().pair("192.168.1.50:37251", "12")
            run.assert_not_called()

    def test_pair_succeeds_on_successfully_paired_output(self):
        with patch(
            "autoperf.adb.subprocess.run",
            return_value=_completed(stdout="Successfully paired to 192.168.1.50:37251\n"),
        ):
            result = AdbClient().pair("192.168.1.50:37251", "123456")
        self.assertIn("paired", result.lower())

    def test_pair_raises_when_adb_reports_failure_despite_exit_zero(self):
        with patch("autoperf.adb.subprocess.run", return_value=_completed(stdout="Failed: wrong pairing code\n")):
            with self.assertRaises(AdbError):
                AdbClient().pair("192.168.1.50:37251", "000000")


if __name__ == "__main__":
    unittest.main()
