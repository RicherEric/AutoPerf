import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from livescreen.server import (
    _PATH_RE,
    _h264_stream,
    _kill_stale_screenrecord,
    _recording_paths,
    _screenshot_stream,
    _start_recording,
    _stop_recording,
    handler,
)

SPS = b"\x67\x64\x00\x34\xaa"
IDR = b"\x65\x88\x84\xab\xab"
DELTA = b"\x41\x9a\xcd"


def annexb(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


class PathRegexTests(unittest.TestCase):
    def test_matches_valid_serial(self):
        match = _PATH_RE.match("/stream/R5CXC006TZD")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("serial"), "R5CXC006TZD")

    def test_rejects_missing_serial(self):
        self.assertIsNone(_PATH_RE.match("/stream/"))

    def test_rejects_unrelated_path(self):
        self.assertIsNone(_PATH_RE.match("/other"))


class FakeWebSocket:
    def __init__(self, path):
        self.request = MagicMock(path=path)
        self.remote_address = ("127.0.0.1", 12345)
        self.sent = []
        self.close = AsyncMock()

    async def send(self, data):
        self.sent.append(data)


class HandlerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_with_1008_for_unrecognized_path(self):
        ws = FakeWebSocket("/nonsense")
        await handler(ws)
        ws.close.assert_awaited_once()
        self.assertEqual(ws.close.call_args.kwargs["code"], 1008)

    async def test_closes_with_1008_for_a_serial_with_disallowed_characters(self):
        # _PATH_RE's charset is identical to exec_out_args's own validation
        # regex, so anything that reaches the ValueError branch would already
        # have failed to match here first -- this exercises that same
        # rejection via the path-matching branch.
        ws = FakeWebSocket("/stream/bad$serial")
        await handler(ws)
        ws.close.assert_awaited_once()
        self.assertEqual(ws.close.call_args.kwargs["code"], 1008)

    async def test_new_connection_awaits_old_connections_cleanup_before_streaming(self):
        # Regression test for a real, empirically-observed bug: cancel()
        # alone only *schedules* cancellation -- the old stream's cleanup
        # (which includes _kill_stale_screenrecord, itself needed to avoid a
        # DIFFERENT orphaned-process bug) runs asynchronously afterwards. A
        # new connection that doesn't wait for that to actually finish can
        # start a fresh screenrecord while the old one still holds Android's
        # single screen-capture slot -- the "needs several clicks on Connect
        # to succeed" symptom this fix addresses.
        order = []
        calls = {"n": 0}

        async def fake_stream(websocket, adb, serial, run_id=None):
            calls["n"] += 1
            n = calls["n"]
            order.append(f"start-{n}")
            if n == 1:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    order.append("cancelled-1")
                    await asyncio.sleep(0.02)  # simulate cleanup taking a moment
                    order.append("cleanup-done-1")
                    raise
            else:
                order.append(f"done-{n}")

        with patch("livescreen.server._h264_stream", fake_stream):
            ws1 = FakeWebSocket("/stream/S1")
            task1 = asyncio.ensure_future(handler(ws1))
            await asyncio.sleep(0.01)  # let the first handler start streaming

            ws2 = FakeWebSocket("/stream/S1")
            await handler(ws2)
            await task1

        self.assertEqual(order, ["start-1", "cancelled-1", "cleanup-done-1", "start-2", "done-2"])

    async def test_a_different_serial_does_not_cancel_an_in_progress_stream(self):
        # Regression test: streams used to be tracked by a single global
        # task, so watching device B would cancel device A's still-running
        # stream even though they're unrelated. Now keyed per-serial.
        order = []

        async def fake_stream(websocket, adb, serial, run_id=None):
            order.append(f"start-{serial}")
            try:
                await asyncio.sleep(10)
                order.append(f"finished-{serial}")
            except asyncio.CancelledError:
                order.append(f"cancelled-{serial}")
                raise

        with patch("livescreen.server._h264_stream", fake_stream):
            ws1 = FakeWebSocket("/stream/S1")
            task1 = asyncio.ensure_future(handler(ws1))
            await asyncio.sleep(0.01)

            ws2 = FakeWebSocket("/stream/S2")
            task2 = asyncio.ensure_future(handler(ws2))
            await asyncio.sleep(0.01)

            self.assertFalse(task1.done())
            task1.cancel()
            task2.cancel()
            for task in (task1, task2):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Both must have started before either was cancelled -- the bug this
        # guards against is S2 starting causing an immediate "cancelled-S1"
        # to appear here, ahead of (or instead of) "start-S2".
        self.assertEqual(order[:2], ["start-S1", "start-S2"])


def _fake_process(stdout_chunks):
    process = MagicMock()
    process.returncode = None
    chunks = iter(stdout_chunks)

    async def read(_n):
        return next(chunks, b"")

    process.stdout = MagicMock()
    process.stdout.read = read
    process.terminate = MagicMock()
    process.wait = AsyncMock()
    return process


class H264StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_key_unit_then_delta_unit_with_correct_prefixes(self):
        stream = annexb(SPS, IDR, DELTA) + b"\x00\x00\x00\x01"
        process = _fake_process([stream, b""])
        ws = FakeWebSocket("/stream/S1")

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)), \
             patch("livescreen.server._kill_stale_screenrecord", AsyncMock()):
            adb = MagicMock()
            adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screenrecord ..."]
            await _h264_stream(ws, adb, "S1")

        self.assertEqual(len(ws.sent), 2)
        self.assertEqual(ws.sent[0][:1], b"\x01")  # key unit prefix
        self.assertEqual(ws.sent[1][:1], b"\x00")  # delta unit prefix

    async def test_kills_stale_remote_screenrecord_before_and_after_streaming(self):
        # Regression test for a real, empirically-observed bug: killing the
        # local adb exec-out client does not reliably kill the remote
        # on-device screenrecord process, which then holds Android's single
        # screen-capture slot and starves the *next* stream attempt of any
        # output. Both the pre-stream cleanup (clears a prior orphan) and the
        # post-stream cleanup (avoid leaving a new one behind) must run.
        stream = annexb(SPS, IDR) + b"\x00\x00\x00\x01"
        process = _fake_process([stream, b""])
        ws = FakeWebSocket("/stream/S1")
        kill_mock = AsyncMock()

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)), \
             patch("livescreen.server._kill_stale_screenrecord", kill_mock):
            adb = MagicMock()
            adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screenrecord ..."]
            await _h264_stream(ws, adb, "S1")

        self.assertEqual(kill_mock.await_count, 2)
        kill_mock.assert_any_await(adb, "S1")

    async def test_with_run_id_tees_chunks_to_the_recording_process(self):
        stream = annexb(SPS, IDR, DELTA) + b"\x00\x00\x00\x01"
        process = _fake_process([stream, b""])
        ws = FakeWebSocket("/stream/S1")
        recorder = MagicMock()
        recorder.stdin = MagicMock()
        recorder.stdin.drain = AsyncMock()

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)), \
             patch("livescreen.server._kill_stale_screenrecord", AsyncMock()), \
             patch("livescreen.server._start_recording", AsyncMock(return_value=recorder)) as start_mock, \
             patch("livescreen.server._stop_recording", AsyncMock()) as stop_mock:
            adb = MagicMock()
            adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screenrecord ..."]
            await _h264_stream(ws, adb, "S1", run_id="run123")

        start_mock.assert_awaited_once_with("run123")
        self.assertTrue(recorder.stdin.write.called)
        stop_mock.assert_awaited_once_with(recorder, "run123")

    async def test_without_run_id_never_starts_recording(self):
        stream = annexb(SPS, IDR) + b"\x00\x00\x00\x01"
        process = _fake_process([stream, b""])
        ws = FakeWebSocket("/stream/S1")

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)), \
             patch("livescreen.server._kill_stale_screenrecord", AsyncMock()), \
             patch("livescreen.server._start_recording", AsyncMock()) as start_mock:
            adb = MagicMock()
            adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screenrecord ..."]
            await _h264_stream(ws, adb, "S1")

        start_mock.assert_not_called()

    async def test_kill_stale_screenrecord_ignores_a_failing_subprocess(self):
        # pkill exits non-zero when there's nothing to kill -- that's the
        # normal case, not an error, and must never propagate.
        adb = MagicMock()
        adb.executable = "adb"
        broken_process = MagicMock()
        broken_process.wait = AsyncMock(side_effect=OSError("boom"))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=broken_process)):
            await _kill_stale_screenrecord(adb, "S1")  # must not raise


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_recording_returns_none_when_ffmpeg_missing(self):
        with patch("livescreen.server.shutil.which", return_value=None):
            result = await _start_recording("run123")
        self.assertIsNone(result)

    async def test_start_recording_spawns_ffmpeg_remux_when_available(self):
        fake_process = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("livescreen.server.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("livescreen.server.asyncio.create_subprocess_exec",
                       AsyncMock(return_value=fake_process)) as spawn_mock, \
                 patch("livescreen.server.RECORDINGS_ROOT", Path(tmp) / "recordings"):
                result = await _start_recording("run123")

        self.assertIs(result, fake_process)
        args = spawn_mock.call_args.args
        self.assertIn("ffmpeg", args)
        self.assertIn("-c", args)
        self.assertIn("-use_wallclock_as_timestamps", args)
        # Written to a .part path, not the final name directly -- see
        # _stop_recording's atomic-rename tests below for why.
        self.assertTrue(args[-1].endswith("run123.mp4.part"))

    async def test_stop_recording_renames_partial_file_to_final_name_on_clean_exit(self):
        # Regression test: a reader (GET /api/runs/<id>/recording) checking
        # the *final* .mp4 name must never see a file ffmpeg is still
        # writing -- that produced the real, empirically-observed "0:00
        # duration, spins forever" browser symptom (a real file existed, but
        # with no moov atom/trailer yet). Renaming only after a clean exit
        # makes the final path all-or-nothing.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("livescreen.server.RECORDINGS_ROOT", Path(tmp)):
                final_path, partial_path = _recording_paths("run123")
                partial_path.write_bytes(b"fake mp4 bytes")
                recorder = MagicMock()
                recorder.wait = AsyncMock()
                recorder.returncode = 0

                await _stop_recording(recorder, "run123")

                recorder.stdin.close.assert_called_once()
                recorder.wait.assert_awaited_once()
                self.assertFalse(partial_path.exists())
                self.assertTrue(final_path.exists())
                self.assertEqual(final_path.read_bytes(), b"fake mp4 bytes")

    async def test_stop_recording_discards_partial_file_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("livescreen.server.RECORDINGS_ROOT", Path(tmp)):
                final_path, partial_path = _recording_paths("run123")
                partial_path.write_bytes(b"broken mp4 bytes")
                recorder = MagicMock()
                recorder.wait = AsyncMock()
                recorder.returncode = 1

                await _stop_recording(recorder, "run123")

                self.assertFalse(partial_path.exists())
                self.assertFalse(final_path.exists())

    async def test_stop_recording_handles_none(self):
        await _stop_recording(None, "run123")  # must not raise


class ScreenshotStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_png_screenshot_bytes_and_loops(self):
        process = MagicMock()
        process.communicate = AsyncMock(return_value=(b"\x89PNGfake-png", b""))
        ws = FakeWebSocket("/stream/S1")
        adb = MagicMock()
        adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screencap -p"]

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)):
            task = asyncio.ensure_future(_screenshot_stream(ws, adb, "S1", interval=0.01))
            await asyncio.sleep(0.03)  # let one iteration run
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0], b"\x89PNGfake-png")


if __name__ == "__main__":
    unittest.main()
