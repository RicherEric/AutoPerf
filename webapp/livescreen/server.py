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
        # `-f mp4` on the *output* is not redundant: ffmpeg chooses the muxer
        # from the filename's extension, and this file is deliberately named
        # `<run>.mp4.part` so no reader can open it before it is finalised --
        # which makes the extension `.part`. ffmpeg refuses ("Unable to
        # choose an output format"), exits -22, and the recording is dropped.
        # Recording had therefore never once succeeded, on any device; the
        # symptom was the UI's "no recording for this run", which reads as a
        # thing that did not happen rather than a thing that failed.
        "-c", "copy", "-f", "mp4", str(partial_path),
        # stderr is captured, not discarded: when ffmpeg fails, the exit
        # code alone ("-22") says nothing, and the recording is then dropped
        # with no way to find out why. Its own message is the only evidence.
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
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
        detail = ""
        if recorder.stderr is not None:
            try:
                detail = (await asyncio.wait_for(recorder.stderr.read(), timeout=2)
                          ).decode("utf-8", "replace").strip()
            except (asyncio.TimeoutError, Exception):
                detail = ""
        logger.warning("ffmpeg exited %s for run %s -- discarding partial recording%s",
                        recorder.returncode, run_id, f": {detail}" if detail else "")
        partial_path.unlink(missing_ok=True)


# How long the stream must be silent before the NAL still in the splitter is
# treated as complete and sent anyway. `screenrecord` emits a frame only when
# the screen changes, so on a device sitting still there is no "next frame" to
# terminate the first one -- and without this the preview stays black until
# something moves on a device you were opening the preview in order to move.
# A second is orders of magnitude longer than the gap inside one frame's write
# to a pipe, so a NAL this old is complete rather than half-written.
IDLE_FLUSH_SECONDS = 1.0


async def _emit(websocket, assembler: AccessUnitAssembler, nals) -> None:
    """Assembles NALs into access units and sends whichever ones complete.

    The leading byte tells the browser which kind it is, because WebCodecs
    needs `type: 'key'` on the chunk carrying SPS+PPS+IDR and would otherwise
    have to parse the bitstream itself to find out.
    """
    for nal_type, payload in nals:
        result = assembler.feed(nal_type, payload)
        if result is None:
            continue
        is_key, framed = result
        await websocket.send((b"\x01" if is_key else b"\x00") + framed)


async def _h264_stream(websocket, adb: AdbClient, serial: str, run_id: str | None = None) -> None:
    await _kill_stale_screenrecord(adb, serial)
    argv = adb.exec_out_args(serial, "screenrecord --output-format=h264 --time-limit=0 -")
    splitter = AnnexBSplitter()
    assembler = AccessUnitAssembler()
    process = await _spawn(argv)
    recorder = await _start_recording(run_id) if run_id else None
    checked_preamble = False
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(65536), timeout=IDLE_FLUSH_SECONDS
                )
            except asyncio.TimeoutError:
                # Quiet stream: whatever the splitter is holding is a whole
                # frame, not a partial one. Send it rather than wait for a
                # screen change that may never come.
                await _emit(websocket, assembler, splitter.flush())
                continue
            if not chunk:
                break
            if not checked_preamble:
                # screenrecord reports its own failures on *stdout*, ahead of
                # the bitstream, and then carries on at a lower resolution:
                # "ERROR: unable to configure video/avc codec at 2048x1280
                # (err=-22) / WARNING: failed at 2048x1280, retrying at
                # 1280x720". The splitter skips it as not-a-start-code, which
                # is right but silent -- and it is the only explanation of why
                # a device's preview is not its real resolution.
                checked_preamble = True
                end = chunk.find(b"\x00\x00\x01")
                if end > 0 and chunk[end - 1] == 0:
                    end -= 1  # the leading zero of a 4-byte start code
                preamble = chunk if end == -1 else chunk[:end]
                if preamble.strip():
                    logger.warning("%s: screenrecord said: %s", serial,
                                   preamble.decode("utf-8", "replace").strip())
                # And it must not reach ffmpeg. The splitter skips it, but the
                # recorder is teed the raw bytes -- and over a pipe ffmpeg
                # gets that text as its entire first read (66 bytes on the
                # Redmi Pad, the bitstream not arriving until the next chunk),
                # fails to parse it as H.264 and exits -22 before any video
                # exists. Every recording on that device was discarded for
                # this, which read as "no recording was made" rather than as
                # a failure. Feeding a *file* with the same preamble works,
                # which is why it only ever showed up on the live path.
                chunk = chunk[end:] if end > 0 else chunk
            if recorder is not None:
                recorder.stdin.write(chunk)
                await recorder.stdin.drain()
            await _emit(websocket, assembler, splitter.feed(chunk))
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        await _kill_stale_screenrecord(adb, serial)
        await _stop_recording(recorder, run_id)


async def _resize_screenshot(data: bytes, max_width: int) -> bytes:
    """Downscale and JPEG-compress a PNG before it crosses the WebSocket.

    Stock Android ``screencap`` cannot resize at capture time.  ffmpeg is
    already an optional runtime dependency of this service for recordings,
    and lets us cut a 2-4 MB TV PNG to a small monitoring frame without
    adding a heavyweight image library to the project.
    """
    if max_width <= 0 or shutil.which("ffmpeg") is None:
        return data
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error",
        "-i", "pipe:0",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-frames:v", "1",
        "-q:v", "5",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    resized, _ = await process.communicate(data)
    return resized if process.returncode == 0 and resized else data


async def _screenshot_stream(
    websocket,
    adb: AdbClient,
    serial: str,
    interval: float = 0.7,
    max_width: int = 0,
) -> None:
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
            data = await _resize_screenshot(data, max_width)
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
    try:
        screenshot_interval = min(5.0, max(0.4, float(query.get("interval", ["0.7"])[0])))
    except (TypeError, ValueError):
        screenshot_interval = 0.7
    try:
        screenshot_max_width = min(1920, max(0, int(query.get("max_width", ["0"])[0])))
    except (TypeError, ValueError):
        screenshot_max_width = 0
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
            await _screenshot_stream(
                websocket, adb, serial, screenshot_interval, screenshot_max_width
            )
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
