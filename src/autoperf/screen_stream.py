from __future__ import annotations

NAL_NON_IDR = 1
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8

_START_CODE = b"\x00\x00\x00\x01"


def _find_start_code(buf: bytes, start: int) -> tuple[int, int] | None:
    """Finds the next Annex-B start code (3- or 4-byte form) at or after `start`.

    Returns (offset, length) or None if no start code is present yet.
    """
    idx = buf.find(b"\x00\x00\x01", start)
    if idx == -1:
        return None
    if idx > 0 and buf[idx - 1] == 0:
        return idx - 1, 4
    return idx, 3


class AnnexBSplitter:
    """Splits an arbitrary, chunk-boundary-agnostic Annex-B byte stream into NAL units.

    Pure and I/O-free: callers (the live-screen server) feed it raw bytes read
    from `adb exec-out screenrecord --output-format=h264 -`'s stdout, in
    whatever chunk sizes the pipe happens to deliver.
    """

    def __init__(self):
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        self._buffer += chunk
        nals: list[tuple[int, bytes]] = []
        while True:
            first = _find_start_code(self._buffer, 0)
            if first is None:
                break
            first_offset, first_len = first
            search_from = first_offset + first_len
            second = _find_start_code(self._buffer, search_from)
            if second is None:
                self._buffer = self._buffer[first_offset:]
                break
            second_offset, _ = second
            payload = self._buffer[search_from:second_offset]
            if payload:
                nals.append((payload[0] & 0x1F, payload))
            self._buffer = self._buffer[second_offset:]
        return nals


class AccessUnitAssembler:
    """Groups a stream of (nal_type, payload) NALs into WebCodecs-ready access units.

    A key access unit bundles SPS+PPS+IDR together -- what WebCodecs' `annexb`
    bitstream format expects for a `type: 'key'` EncodedVideoChunk (the spec
    requires the parameter sets and the IDR picture in the same chunk). A
    delta access unit is just one non-IDR slice NAL. Other NAL types (SEI,
    AUD, ...) are dropped -- not needed to decode a live preview.
    """

    def __init__(self):
        self._pending_params: list[bytes] = []

    def feed(self, nal_type: int, payload: bytes) -> tuple[bool, bytes] | None:
        if nal_type in (NAL_SPS, NAL_PPS):
            self._pending_params.append(payload)
            return None
        if nal_type == NAL_IDR:
            framed = b"".join(_START_CODE + p for p in self._pending_params) + _START_CODE + payload
            self._pending_params = []
            return True, framed
        if nal_type == NAL_NON_IDR:
            return False, _START_CODE + payload
        return None
