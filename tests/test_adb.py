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


if __name__ == "__main__":
    unittest.main()
