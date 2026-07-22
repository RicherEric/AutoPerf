from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import serve

from autoperf.adb import AdbClient
from autoperf.screen_stream import AccessUnitAssembler, AnnexBSplitter

logger = logging.getLogger("autoperf.livescreen")

_PATH_RE = re.compile(r"^/stream/(?P<serial>[A-Za-z0-9._:-]+)$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Same directory Django serves recordings from (RECORDINGS_ROOT in
# webapp/config/settings.py) -- webapp/recordings/, resolved relative to
# this file rather than the process's CWD so it's correct regardless of
# where `python -m livescreen.server` was launched from.
RECORDINGS_ROOT = Path(__file__).resolve().parent.parent / "recordings"

# One active stream per device at a time (last-connect-wins) -- this is a
# local, single-developer tool with no auth; a new connection to a given
# serial simply cancels whatever stream is currently running for *that*
# serial rather than sharing/queuing. Keyed by serial (not a single global)
# so watching two different devices at once -- e.g. two Run Detail tabs for
# two different in-progress runs -- doesn't have one cancel the other.
_active_tasks: dict[str, asyncio.Task] = {}


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


def _recording_paths(run_id: str) -> tuple[Path, Path]:
    """(final_path, partial_path) -- recording is written to partial_path and
    only renamed to final_path once ffmpeg has actually exited cleanly (see
    _stop_recording). Never expose partial_path to readers: a reader
    (GET /api/runs/<id>/recording) that opened it mid-write would see a file
    with no moov atom/trailer yet -- a real MP4 file that exists but has no
    valid duration or sample index, which is exactly the browser's "0:00,
    spins forever" symptom for a <video> that can never finish loading."""
    return RECORDINGS_ROOT / f"{run_id}.mp4", RECORDINGS_ROOT / f"{run_id}.mp4.part"


async def _start_recording(run_id: str) -> asyncio.subprocess.Process | None:
    """Tees the raw H.264 elementary stream into an MP4 via ffmpeg (`-c copy`,
    a cheap remux with no re-encode -- no re-parsing of NAL units needed on
    our end) so a finished run gets a real, seekable `<video>` to scrub.

    Deliberately reuses the *existing* live-stream's screenrecord process
    rather than spawning a second, independent screen-capture session --
    Android only allows one screen-capture session per device at a time, so
    a separate always-on recorder would fight the live view for that slot.
    The tradeoff: a run only gets recorded for however much of it someone
    had the live panel open for (in practice, the whole run, since Run
    Detail's panel auto-connects the moment the page opens).

    `-use_wallclock_as_timestamps` matters because a raw H.264 elementary
    stream carries no timing info of its own (no PTS/DTS) -- without it,
    ffmpeg has to guess a constant frame rate for the whole file, which can
    produce a wildly wrong (including near-zero) duration; timestamping each
    chunk as it actually arrives makes the muxed file's duration match how
    long the recording really ran.

    Returns None (recording silently skipped, live streaming unaffected) if
    ffmpeg isn't installed.
    """
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not found on PATH -- skipping recording for run %s", run_id)
        return None
    RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
    _, partial_path = _recording_paths(run_id)
    return await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "h264", "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
        "-c", "copy", str(partial_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


async def _stop_recording(recorder: asyncio.subprocess.Process | None, run_id: str | None) -> None:
    if recorder is None:
        return
    final_path, partial_path = _recording_paths(run_id)
    try:
        if recorder.stdin is not None:
            recorder.stdin.close()
        await asyncio.wait_for(recorder.wait(), timeout=10)
    except Exception:
        logger.warning("ffmpeg recording process didn't exit cleanly for run %s", run_id)
        return
    if recorder.returncode == 0 and partial_path.exists():
        partial_path.replace(final_path)  # atomic on the same filesystem
    else:
        logger.warning("ffmpeg exited %s for run %s -- discarding partial recording",
                        recorder.returncode, run_id)
        partial_path.unlink(missing_ok=True)


async def _h264_stream(websocket, adb: AdbClient, serial: str, run_id: str | None = None) -> None:
    await _kill_stale_screenrecord(adb, serial)
    argv = adb.exec_out_args(serial, "screenrecord --output-format=h264 --time-limit=0 -")
    splitter = AnnexBSplitter()
    assembler = AccessUnitAssembler()
    process = await _spawn(argv)
    recorder = await _start_recording(run_id) if run_id else None
    try:
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break
            if recorder is not None:
                recorder.stdin.write(chunk)
                await recorder.stdin.drain()
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
        await _stop_recording(recorder, run_id)


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
    parsed = urlparse(websocket.request.path)
    match = _PATH_RE.match(parsed.path)
    if not match:
        await websocket.close(code=1008, reason="expected path /stream/<serial>")
        return
    serial = match.group("serial")
    query = parse_qs(parsed.query)
    mode = query.get("mode", ["h264"])[0]
    run_id = query.get("run_id", [None])[0]
    if run_id is not None and not _RUN_ID_RE.fullmatch(run_id):
        run_id = None
    adb = AdbClient()

    previous = _active_tasks.get(serial)
    if previous is not None and not previous.done():
        previous.cancel()
        # Cancellation only takes effect at the old task's next await point,
        # and its cleanup (process.terminate()/wait(), _kill_stale_screenrecord)
        # runs asynchronously after that -- proceeding immediately without
        # waiting for it to actually finish is a real race: a fresh Connect
        # can start spawning a new screenrecord while the old one is still
        # mid-teardown and still holding Android's single screen-capture
        # slot, intermittently starving the new attempt of any output (the
        # empirically observed "needs several clicks to succeed" symptom).
        try:
            await previous
        except Exception:
            pass
    _active_tasks[serial] = asyncio.current_task()

    logger.info("streaming %s to %s (mode=%s, run_id=%s)", serial, websocket.remote_address, mode, run_id)
    started = time.monotonic()
    try:
        if mode == "screenshot":
            await _screenshot_stream(websocket, adb, serial)
        else:
            await _h264_stream(websocket, adb, serial, run_id)
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
