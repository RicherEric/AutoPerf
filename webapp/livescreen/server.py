from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import serve

from autoperf.adb import AdbClient
from autoperf.screen_stream import AccessUnitAssembler, AnnexBSplitter

logger = logging.getLogger("autoperf.livescreen")

_PATH_RE = re.compile(r"^/stream/(?P<serial>[A-Za-z0-9._:-]+)$")

# One active stream at a time (last-connect-wins) -- this is a local,
# single-developer tool with no auth; a new connection simply cancels
# whatever stream is currently running rather than sharing/queuing.
_active_task: asyncio.Task | None = None


async def _spawn(argv: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )


async def _kill_stale_screenrecord(adb: AdbClient, serial: str) -> None:
    """Terminating the local `adb exec-out screenrecord` client process does
    not reliably kill the *remote* on-device screenrecord process -- adb
    doesn't always propagate that termination over the transport. A leftover
    remote process holds Android's single screen-capture slot, so the next
    stream attempt gets zero output and silently times out client-side (the
    "no video frame arrived" fallback) despite streaming working moments
    earlier. Called both before starting a new stream (clean up any orphan
    from a prior crashed/killed session) and after stopping one (make this
    session's own remote process doesn't become the next orphan). Safe to
    call when nothing is running -- pkill's no-match exit is not an error.
    """
    argv = [adb.executable, "-s", serial, "shell", "pkill -f screenrecord"]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        pass


async def _h264_stream(websocket, adb: AdbClient, serial: str) -> None:
    await _kill_stale_screenrecord(adb, serial)
    argv = adb.exec_out_args(serial, "screenrecord --output-format=h264 --time-limit=0 -")
    splitter = AnnexBSplitter()
    assembler = AccessUnitAssembler()
    process = await _spawn(argv)
    try:
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break
            for nal_type, payload in splitter.feed(chunk):
                result = assembler.feed(nal_type, payload)
                if result is None:
                    continue
                is_key, framed = result
                prefix = b"\x01" if is_key else b"\x00"
                await websocket.send(prefix + framed)
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        await _kill_stale_screenrecord(adb, serial)


async def _screenshot_stream(websocket, adb: AdbClient, serial: str, interval: float = 0.7) -> None:
    """Fallback for browsers without WebCodecs: periodic PNG screenshots.

    `adb shell screencap -p` outputs PNG (not JPEG, despite the `-p` flag's
    name suggesting otherwise) -- there is no JPEG option in stock AOSP
    screencap, so the client must decode these as image/png.
    """
    argv = adb.exec_out_args(serial, "screencap -p")
    while True:
        process = await _spawn(argv)
        data, _ = await process.communicate()
        if data:
            await websocket.send(data)
        await asyncio.sleep(interval)


async def handler(websocket) -> None:
    global _active_task

    parsed = urlparse(websocket.request.path)
    match = _PATH_RE.match(parsed.path)
    if not match:
        await websocket.close(code=1008, reason="expected path /stream/<serial>")
        return
    serial = match.group("serial")
    mode = parse_qs(parsed.query).get("mode", ["h264"])[0]
    adb = AdbClient()

    if _active_task is not None and not _active_task.done():
        _active_task.cancel()
        # Cancellation only takes effect at the old task's next await point,
        # and its cleanup (process.terminate()/wait(), _kill_stale_screenrecord)
        # runs asynchronously after that -- proceeding immediately without
        # waiting for it to actually finish is a real race: a fresh Connect
        # can start spawning a new screenrecord while the old one is still
        # mid-teardown and still holding Android's single screen-capture
        # slot, intermittently starving the new attempt of any output (the
        # empirically observed "needs several clicks to succeed" symptom).
        try:
            await _active_task
        except Exception:
            pass
    _active_task = asyncio.current_task()

    stream_fn = _screenshot_stream if mode == "screenshot" else _h264_stream
    logger.info("streaming %s to %s (mode=%s)", serial, websocket.remote_address, mode)
    started = time.monotonic()
    try:
        await stream_fn(websocket, adb, serial)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
        pass
    finally:
        logger.info("stream ended for %s after %.1fs", serial, time.monotonic() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoperf-livescreen")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def run() -> None:
        async with serve(handler, args.host, args.port):
            logger.info("livescreen server listening on %s:%s", args.host, args.port)
            await asyncio.Future()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
