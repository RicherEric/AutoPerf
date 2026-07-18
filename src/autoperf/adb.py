from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .models import Device


class AdbError(RuntimeError):
    pass


class AdbClientProtocol(Protocol):
    def devices(self) -> list[Device]: ...
    def shell(self, serial: str, command: str, timeout: float = 10) -> str: ...


@dataclass(slots=True)
class AdbClient:
    executable: str = "adb"

    def _run(self, *args: str, timeout: float = 15) -> str:
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"ADB executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {timeout}s") from exc
        if result.returncode:
            raise AdbError(result.stderr.strip() or f"ADB exited with {result.returncode}")
        return result.stdout

    def devices(self) -> list[Device]:
        lines = self._run("devices", "-l").splitlines()[1:]
        found: list[Device] = []
        for line in lines:
            if not line.strip():
                continue
            parts = line.split()
            metadata = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            found.append(Device(parts[0], parts[1], metadata.get("model", "unknown"),
                                metadata.get("product", "unknown"), metadata.get("transport_id")))
        return found

    def connect(self, address: str, timeout: float = 10) -> str:
        """Connects to a device exposed over adb-over-WiFi (`adb connect <ip>:<port>`).

        Used for the classroom-demo scenario where students join over WiFi
        instead of plugging in via USB. `adb connect` itself always exits 0
        even on failure (e.g. "cannot connect to ..."), so failures are
        detected by inspecting stdout rather than the return code.
        """
        if not re.fullmatch(r"[A-Za-z0-9.\-]+:[0-9]{1,5}", address):
            raise ValueError("Address must look like <host>:<port>, e.g. 192.168.1.50:5555")
        output = self._run("connect", address, timeout=timeout)
        if "connected to" not in output.lower():
            raise AdbError(output.strip() or f"failed to connect to {address}")
        return output.strip()

    def pair(self, address: str, code: str, timeout: float = 10) -> str:
        """One-time pairing for adb-over-WiFi (Android 11+'s "Wireless
        debugging -> Pair device with pairing code"), fully USB-free.

        `address` is the pairing IP:port shown on that screen -- a
        *different* port than the one later used for `connect()` above, by
        Android's own design. `code` is the 6-digit pairing code shown
        alongside it. Like connect(), `adb pair` exits 0 even on a wrong
        code, so success is detected from stdout content.
        """
        if not re.fullmatch(r"[A-Za-z0-9.\-]+:[0-9]{1,5}", address):
            raise ValueError("Address must look like <host>:<port>, e.g. 192.168.1.50:37251")
        if not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("Pairing code must be the 6-digit code shown on the device")
        output = self._run("pair", address, code, timeout=timeout)
        if "successfully paired" not in output.lower():
            raise AdbError(output.strip() or f"failed to pair with {address}")
        return output.strip()

    def mdns_services(self, timeout: float = 10) -> dict:
        """Lists devices advertising ADB-over-WiFi via mDNS on the local
        network (`adb mdns services`, Android 11+ adb) -- lets a classroom
        of students' phones be discovered automatically instead of everyone
        hunting through Settings for their own IP:port. A device shows up
        here once "Wireless debugging" is toggled on, whether or not it's
        been paired yet (a `_tcp-tls-pairing._tcp` entry means "ready to
        pair", `_adb-tls-connect._tcp` means "already paired, ready to
        connect").

        Requires the network to actually carry mDNS/multicast between
        devices -- many school or enterprise WiFi networks enable client
        isolation, which silently blocks this and yields an empty list even
        though wireless debugging is genuinely on. `adb mdns services`'
        exact line format isn't a stable, documented contract, so parsing
        here is deliberately defensive: any line it doesn't recognize is
        simply skipped rather than raising, and the raw output is always
        returned alongside the parsed list so nothing is silently lost.
        """
        output = self._run("mdns", "services", timeout=timeout)
        services = []
        for line in output.splitlines()[1:]:  # skip the "List of discovered..." header
            parts = line.split()
            if len(parts) < 2:
                continue
            name, address = parts[0], parts[-1]
            if not re.fullmatch(r"[A-Za-z0-9.\-]+:[0-9]{1,5}", address):
                continue
            kind = "pairing" if "pairing" in name.lower() else "connect" if "connect" in name.lower() else "unknown"
            services.append({"name": name, "address": address, "kind": kind})
        return {"raw": output.strip(), "services": services}

    def shell(self, serial: str, command: str, timeout: float = 10) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", serial):
            raise ValueError("Invalid Android device serial")
        return self._run("-s", serial, "shell", command, timeout=timeout)

    def exec_out_args(self, serial: str, command: str) -> list[str]:
        """Build the argv for `adb exec-out <command>` without running it.

        Used by long-lived/streaming callers (the live-screen server) that
        need to spawn adb themselves via asyncio and read its stdout as a
        live byte stream, rather than through the synchronous, buffered
        `_run`/`shell` path above.
        """
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", serial):
            raise ValueError("Invalid Android device serial")
        return [self.executable, "-s", serial, "exec-out", command]
