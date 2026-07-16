import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from livescreen.server import _PATH_RE, _h264_stream, _screenshot_stream, handler

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

        with patch("livescreen.server._spawn", AsyncMock(return_value=process)):
            adb = MagicMock()
            adb.exec_out_args.return_value = ["adb", "-s", "S1", "exec-out", "screenrecord ..."]
            await _h264_stream(ws, adb, "S1")

        self.assertEqual(len(ws.sent), 2)
        self.assertEqual(ws.sent[0][:1], b"\x01")  # key unit prefix
        self.assertEqual(ws.sent[1][:1], b"\x00")  # delta unit prefix


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
