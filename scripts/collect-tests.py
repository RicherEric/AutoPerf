#!/usr/bin/env python
"""Read the test suite and describe it, without running it.

Every entry in the inventory this produces is extracted from the tests
themselves -- the group a module belongs to, the docstring a test carries, the
doubles its body reaches for. Nothing here is a hand-written list, because a
hand-written list of 500-odd tests is stale the day after it is written, and a
stale inventory that still looks complete is the same class of lie the suite
itself is built to catch.

    python scripts/collect-tests.py            # JSON to stdout
    python scripts/collect-tests.py --webapp   # the Django half only (internal)

Two halves, two processes: the core suite is plain unittest, while the webapp
suite needs Django's settings configured first, and doing that in this process
would leak into the core import. `scripts/run-tests.py` splits them for the
same reason.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_TESTS = ROOT / "scripts" / "run-tests.py"

# What a test reaches for, in the order that reads best when several apply.
# Detected from the body rather than declared, so a test that changes its
# approach cannot keep an inventory entry that describes the old one. The
# fourth field is the glossary term the label corresponds to, or "" -- that is
# what links this inventory to the 名詞解釋 tab.
TECHNIQUES = [
    ("CapturedAdb", "真機擷取重播", "重播從實體裝置錄下來的文字，全套件唯一對照真機輸出的一層", "CapturedAdb"),
    ("RecordingAdb", "指令錄音（Spy）", "斷言送給裝置的指令字串長什麼樣", "RecordingAdb"),
    ("DeviceAdb", "假裝置回覆表（Stub）", "供給「裝置回什麼」，未列出的指令會 raise", "DeviceAdb"),
    ("self.client.", "HTTP API", "用 Django test client 打真正的 URL 路由，不是直接呼叫 view 函式", "API"),
    ("apply_async", "佇列派送", "斷言工作被正確地丟進 Celery，而不是真的執行它", "Django"),
    ("subprocess", "子行程", "把 subprocess 換掉，驗證真正送出去的命令列", "subprocess"),
    ("MagicMock", "mock / patch", "把協作者換成假的，隔離受測的那一層", "mock"),
    ("patch(", "mock / patch", "把協作者換成假的，隔離受測的那一層", "mock"),
    ("Waits(", "等待歸零", "把重試間隔設為 0，只保留「重試幾次」這個行為", "Waits.instant()"),
    ("instant()", "等待歸零", "把重試間隔設為 0，只保留「重試幾次」這個行為", "Waits.instant()"),
    ("assertRaises", "預期例外", "斷言錯誤會被丟出來，而不是被吞掉", "VerificationError"),
    ("TemporaryDirectory", "暫存資料庫", "每個測試一個乾淨的 SQLite 檔，測完就沒了", ""),
    ("NamedTemporaryFile", "暫存資料庫", "每個測試一個乾淨的 SQLite 檔，測完就沒了", ""),
]

PURE = ("純函式", "沒有替身也沒有 I/O：輸入一段資料，斷言算出來的決定", "Humble Object")

# Whether a test is about a device at all. Decided from what the test body
# names, not from which file it lives in -- the same reason the techniques
# above are scanned rather than declared. The second field is shown in the
# inventory as the reason, so the classification can be argued with instead of
# being taken on trust.
#
# The rule being applied: would this test still mean anything if no device
# existed? `test_storage` would. `test_uiauto` would not.
#
# Only strong evidence counts. `serial`, `scenario` and `profile` were tried
# and dropped: a serial is a string, and `test_storage` stores one in a column
# without ever knowing what it is. Flagging those made 39 of 46 storage tests
# "device tests", which is exactly backwards -- storage is the part of this
# system that would still work if devices did not exist.
DEVICE_MARKERS = [
    ("CapturedAdb", "重播真機擷取的文字"),
    ("DeviceAdb", "對假裝置的回覆表斷言"),
    ("RecordingAdb", "斷言送給裝置的指令"),
    ("AdbClient", "把 adb client 換掉"),
    ("adb.shell", "走那唯一的縫隙"),
    ("adb,", "把 adb 傳進受測函式"),
    ("adb=", "把 adb 傳進受測函式"),
    ("(adb", "把 adb 傳進受測函式"),
    ("uiauto", "解析裝置吐出來的畫面結構"),
    ("adapter", "驅動 adapter，也就是裝置動作那一層"),
    ("dumpsys", "讀裝置的系統輸出"),
    ("hierarchy", "畫面元素樹"),
    ("Target(", "UI selector 目標"),
    ("ALL_TARGETS", "UI selector 表"),
    ("selector", "UI selector"),
    ("screenshot", "抓裝置畫面"),
    ("keyevent", "對裝置送按鍵"),
    ("input tap", "對裝置送點擊"),
    ("getprop", "讀裝置屬性"),
]


def load_groups() -> dict[str, tuple[str, tuple[str, ...]]]:
    """The taxonomy comes from run-tests.py, so there is only one of it."""
    spec = importlib.util.spec_from_file_location("run_tests", RUN_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GROUPS


def first_paragraph(text: str | None) -> str:
    if not text:
        return ""
    lines = []
    for line in inspect.cleandoc(text).splitlines():
        if not line.strip():
            break
        lines.append(line.strip())
    return " ".join(lines)


def humanise(method_name: str) -> str:
    return method_name.removeprefix("test_").replace("_", " ").strip()


ALIAS_RE = re.compile(r"^\s*from\s+\S+\s+import\s+(.+)$", re.M)


def aliases_in(module_source: str) -> tuple[list, list]:
    """Follow `import RecordingAdb as ScriptedAdb`.

    Scanning for the real name alone under-reports: `test_collectors_errors`
    imports the Spy under a local name, so every test in it looked like a pure
    function that had never heard of a device. An alias is the same evidence
    wearing a different word.
    """
    techniques, markers = [], []
    for match in ALIAS_RE.finditer(module_source):
        # The comment has to go first: `RecordingAdb as ScriptedAdb  # ...`
        # otherwise reads as an alias named "ScriptedAdb  # ...", which
        # matches nothing.
        for part in match.group(1).split("#")[0].split(","):
            if " as " not in part:
                continue
            original, _, alias = (bit.strip() for bit in part.partition(" as "))
            alias = alias.strip("() ").split()[0] if alias.strip() else ""
            if not alias:
                continue
            techniques += [(alias, label, why, term)
                           for marker, label, why, term in TECHNIQUES if marker == original]
            markers += [(alias, reason) for marker, reason in DEVICE_MARKERS
                        if marker == original]
    return techniques, markers


def _scan(text: str, seen: set[str], extra: list) -> list[dict]:
    found = []
    for marker, label, why, term in [*TECHNIQUES, *extra]:
        if marker in text and label not in seen:
            seen.add(label)
            found.append({"label": label, "why": why, "term": term})
    return found


def techniques_for(source: str, class_source: str, extra: list = ()) -> list[dict]:
    seen: set[str] = set()
    found = _scan(source, seen, list(extra))
    if found:
        return found
    # Nothing in the body: the setup may hold the double for the whole class.
    found = _scan(class_source, seen, list(extra))
    return found or [{"label": PURE[0], "why": PURE[1], "term": PURE[2]}]


def device_verdict(source: str, class_source: str, extra: list = ()) -> tuple[bool, str]:
    """Is this a device test, and on what evidence."""
    for text in (source, class_source):
        lowered = text.lower()
        for marker, reason in [*DEVICE_MARKERS, *extra]:
            if marker.lower() in lowered:
                return True, reason
    return False, ""


def describe_case(cls, class_source: str, group: str, module: str,
                  module_purpose: str, extra_tech=(), extra_markers=()) -> list[dict]:
    loader = unittest.TestLoader()
    items = []
    for name in loader.getTestCaseNames(cls):
        func = getattr(cls, name)
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            source = ""
        device, why_device = device_verdict(source, class_source, extra_markers)
        items.append({
            "group": group,
            "module": module,
            "module_purpose": module_purpose,
            "suite": cls.__name__,
            "suite_purpose": first_paragraph(cls.__doc__),
            "name": name,
            "title": humanise(name),
            "purpose": first_paragraph(func.__doc__),
            "techniques": techniques_for(source, class_source, extra_tech),
            "device": device,
            "why_device": why_device,
        })
    return items


def describe_module(module, group: str, module_label: str) -> list[dict]:
    module_purpose = first_paragraph(module.__doc__)
    try:
        extra_tech, extra_markers = aliases_in(inspect.getsource(module))
    except (OSError, TypeError):
        extra_tech, extra_markers = [], []
    items = []
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if not issubclass(cls, unittest.TestCase) or cls.__module__ != module.__name__:
            continue
        try:
            class_source = inspect.getsource(cls)
        except (OSError, TypeError):
            class_source = ""
        items.extend(describe_case(cls, class_source, group, module_label,
                                   module_purpose, extra_tech, extra_markers))
    return items


def collect_core() -> tuple[list[dict], list[dict]]:
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    groups, items = [], []
    for group, (why, modules) in load_groups().items():
        groups.append({"name": group, "why": why, "kind": "core"})
        for name in modules:
            module = importlib.import_module(f"tests.{name}")
            items.extend(describe_module(module, group, name))
    return groups, items


def collect_webapp_here() -> list[dict]:
    """Runs inside the --webapp subprocess, after Django is configured."""
    sys.path.insert(0, str(ROOT / "webapp"))
    sys.path.insert(0, str(ROOT / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()

    items = []
    for app in ("dashboard", "livescreen"):
        start = ROOT / "webapp" / app
        if not start.exists():
            continue
        for suite in unittest.defaultTestLoader.discover(
            start_dir=str(start), top_level_dir=str(ROOT / "webapp")
        ):
            for case in _flatten(suite):
                module = sys.modules[type(case).__module__]
                parts = type(case).__module__.split(".")
                # `livescreen.tests` is a module, `dashboard.tests.test_runs`
                # is a package member; a bare "tests" names neither.
                label = parts[-1] if parts[-1] != "tests" else ".".join(parts[-2:])
                try:
                    class_source = inspect.getsource(type(case))
                except (OSError, TypeError):
                    class_source = ""
                func = getattr(type(case), case._testMethodName)
                try:
                    source = inspect.getsource(func)
                except (OSError, TypeError):
                    source = ""
                extra_tech, extra_markers = aliases_in(
                    inspect.getsource(module) if module.__file__ else "")
                device, why_device = device_verdict(source, class_source, extra_markers)
                items.append({
                    "group": "webapp",
                    "module": label,
                    "module_purpose": first_paragraph(module.__doc__),
                    "suite": type(case).__name__,
                    "suite_purpose": first_paragraph(type(case).__doc__),
                    "name": case._testMethodName,
                    "title": humanise(case._testMethodName),
                    "purpose": first_paragraph(func.__doc__),
                    "techniques": techniques_for(source, class_source, extra_tech),
                    "device": device,
                    "why_device": why_device,
                })
    return items


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        elif isinstance(item, unittest.TestCase):
            yield item


def collect_webapp() -> tuple[list[dict], str]:
    """The Django half, in its own process. Returns (items, warning)."""
    # Both ends pinned to UTF-8. The default on Windows is cp950 here, which
    # cannot decode the Chinese in the technique labels -- the pipe dies and
    # the inventory silently loses its whole webapp half.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--webapp"],
        capture_output=True, cwd=str(ROOT), env=env,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # Never silently short: a partial inventory that looks whole is worse
        # than one that says what it is missing.
        return [], (result.stderr.strip().splitlines() or ["unknown error"])[-1]
    return json.loads(result.stdout), ""


def collect() -> dict:
    groups, items = collect_core()
    webapp_items, warning = collect_webapp()
    if webapp_items or not warning:
        groups.append({
            "name": "webapp",
            "why": "Django API、Celery 工作、live-screen 串流。API 形狀、任務派送或串流協定改了會壞。",
            "kind": "webapp",
        })
    items.extend(webapp_items)
    return {"groups": groups, "tests": items, "warning": warning}


if __name__ == "__main__":
    if "--webapp" in sys.argv:
        json.dump(collect_webapp_here(), sys.stdout, ensure_ascii=False)
    else:
        inventory = collect()
        json.dump(inventory, sys.stdout, ensure_ascii=False, indent=1)
        print(f"\n{len(inventory['tests'])} tests", file=sys.stderr)
        if inventory["warning"]:
            print(f"webapp half missing: {inventory['warning']}", file=sys.stderr)
