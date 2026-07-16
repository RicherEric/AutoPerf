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
