"""How many Celery workers the launcher starts, and why it is not the device count.

The sizing used to be `min(cpu_count, connected_devices)`: one slot per device,
because a run was the only kind of device job and one worker per device meant
every device could be driven. Preflight changed that -- it is queued the same
way, holds a phone for as long as three minutes, and produces no measurements.
With exactly one worker, a preflight on one phone stalled every other queued
job, including jobs for a device sitting completely idle.

Exclusion moved into the database (`Storage.try_start_run` /
`try_start_preflight`), so the worker count is no longer what keeps two things
off one phone. These pin that reasoning in place: a regression to the old
formula would reintroduce the head-of-line blocking silently, because nothing
would fail -- work would just wait.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "start-worker.py"


def load_script():
    """Imported by path: the file is hyphenated, so it is not a module name."""
    spec = importlib.util.spec_from_file_location("start_worker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start_worker = load_script()


class ChooseConcurrencyTests(unittest.TestCase):
    def size(self, *, cores, devices, explicit=None):
        with patch.object(start_worker.os, "cpu_count", return_value=cores), \
                patch.object(start_worker, "count_connected_devices", return_value=devices):
            return start_worker.choose_concurrency(explicit)

    def test_one_device_gets_more_than_one_worker(self):
        # The case the old formula got wrong: a single phone meant a single
        # worker, so a three-minute preflight was a three-minute queue stall.
        self.assertEqual(self.size(cores=8, devices=1), 2)

    def test_headroom_is_one_slot_beyond_the_devices(self):
        self.assertEqual(self.size(cores=16, devices=3), 4)

    def test_never_more_workers_than_cores(self):
        self.assertEqual(self.size(cores=2, devices=8), 2)

    def test_no_devices_still_starts_a_usable_pair(self):
        # Devices get attached after the worker is up; refusing to start more
        # than one worker because none were plugged in at launch would make the
        # queue's behaviour depend on cable timing.
        self.assertEqual(self.size(cores=8, devices=0), 2)

    def test_no_devices_on_a_single_core_machine_still_starts_one(self):
        self.assertEqual(self.size(cores=1, devices=0), 1)

    def test_explicit_concurrency_wins_over_every_heuristic(self):
        self.assertEqual(self.size(cores=2, devices=1, explicit=6), 6)


class WindowsLaunchTests(unittest.TestCase):
    """N processes, not N threads: `--pool=solo` is the only Windows pool that
    runs a task on its worker's own main thread, which TestRunner.run() needs
    for signal.signal()."""

    def test_each_worker_gets_a_distinct_node_name(self):
        with patch.object(start_worker.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            start_worker.run_windows("celery.exe", 3, "info")
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertEqual(len(commands), 3)
        names = [cmd[cmd.index("-n") + 1] for cmd in commands]
        self.assertEqual(names, ["worker1@%h", "worker2@%h", "worker3@%h"])
        for cmd in commands:
            self.assertIn("--pool=solo", cmd)

    def test_a_single_worker_replaces_the_process_instead_of_spawning(self):
        # No supervisor process for the common case: Ctrl+C reaches the worker
        # directly rather than a parent that has to forward it.
        with patch.object(start_worker.os, "execvp") as execvp:
            start_worker.run_windows("celery.exe", 1, "info")
        execvp.assert_called_once()
        self.assertIn("--pool=solo", execvp.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
