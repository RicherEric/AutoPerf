#!/usr/bin/env python
"""三分鐘現場 demo 的驅動腳本。

三分鐘是一個很硬的限制。play_golden 那支影片本身 3 分 35 秒，smoke suite
四個 scenario 跑完要兩分半，而 Celery 在 Windows 上是 `--pool=solo` ——
一次只執行一個任務，所以「同時跑好幾個測試」在現場根本不會發生，排進去
的東西只會排隊。照著感覺按按鈕，三分鐘一定會用在等待上。

所以這支腳本把 demo 切成兩段，分別對應兩個問題：

    prewarm  —— demo 前 10 分鐘跑一次。基準線、壓力測試的歷史資料、
                selector 失效的證據，全部在這裡產生。現場要「看」的東西
                在這時候就已經存在了。
    live     —— 現場那三分鐘。它在正確的秒數把 run 排進去，並且把當下
                該切到哪一頁、該講哪一段稿印在畫面上。

現場只跑兩個真的要看它動的 run（Golden 與 Groot），其餘都是預熱好的結果。
這不是作弊，是把「等待」跟「講解」錯開：講壓力測試那 40 秒，Groot 正好
在背景跑完。

它透過 HTTP API 操作 dashboard，跟你在瀏覽器上按按鈕走的是同一條路
(`POST /api/runs`、`POST /api/campaigns`)，所以現場看到的東西跟平常一模
一樣。只用標準函式庫，不需要額外安裝任何東西。

    python scripts/demo.py check            # 前置檢查，四個服務跟手機都在嗎
    python scripts/demo.py prewarm          # demo 前 10 分鐘跑，約 5-6 分鐘
    python scripts/demo.py live             # 現場：照時間軸自動排 run + 提詞
    python scripts/demo.py live --manual    # 改成按 Enter 才進下一段
    python scripts/demo.py live --dry-run   # 排練用，不真的送出任何 run

完整的流程稿、逐字講稿與失敗時的 Plan B 在 docs/DEMO_SCRIPT_zh-TW.md。
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_UI = "http://127.0.0.1:5173"
LIVESCREEN_HOST, LIVESCREEN_PORT = "127.0.0.1", 8100

# 一個 run 從「排進佇列」到「真的開始」之間的空檔：worker 取件、adb 探測
# 裝置類型、量到螢幕尺寸。實測大約 3-6 秒，時間軸的每一段都留了這個餘裕。
ENQUEUE_OVERHEAD_HINT = 6.0

# 取樣結束到 run 標成 completed 之間：writer thread 把最後一批樣本寫完並
# join（storage.py 的 writer 是 non-daemon，正是為了不掉最後一批）。
RUN_TEARDOWN_HINT = 3.0


# --- 時間軸的積木 -----------------------------------------------------------

@dataclass(frozen=True)
class Run:
    """一次要排進去的測試。`key` 只是給時間軸內部互相參照用的名字。"""
    scenario: str
    duration: float
    key: str = ""
    blind: tuple[str, ...] = ()


@dataclass(frozen=True)
class Campaign:
    """一個長時間測試。repeat 是「同一個 scenario 反覆跑」，量的是穩定度。"""
    kind: str
    scenario: str
    duration: float
    iterations: int
    key: str = ""


@dataclass(frozen=True)
class Beat:
    """時間軸上的一段。

    `at` 是目標秒數，不是保證 —— 如果 `wait_run` 指定的 run 還沒跑完，這一段
    會等它。寧可晚 10 秒講，也不要切過去發現結果還沒出來。
    """
    at: float
    page: str
    cue: str
    say: str
    start: Run | Campaign | None = None
    wait_run: str = ""


# --- 現場那三分鐘 -----------------------------------------------------------
#
# 時間軸的排法有一個限制在支配它：solo worker 一次只跑一個 run，所以
# 「排進去」跟「開始跑」是兩件事。Groot 在第 98 秒就排進去，但那時 Golden
# 還在跑；它會在 Golden 結束後自動接上，而那 47 秒我們正在講壓力測試。
# 講完切回去（第 145 秒），結果剛好在那裡——Groot 約在 2:17 跑完，餘裕 8 秒。
# 這個「餘裕 8 秒」不是估的，`demo.py budget` 會把佇列模擬出來檢查它。
#
# 每一段的 `say` 是逐字稿。中文口語約 3.3 字/秒，而且不能講滿——手機自己
# 亮起來、影片開始播的那幾秒要留白讓台下看。第一版的稿念完要 308 秒，硬塞在
# 180 秒的軸上，每一段都超過十幾秒；超時在台上不會有人提醒你，只會變成後面
# 全部趕。所以這件事要用算的：改完稿跑一次 `python scripts/demo.py budget`。

LIVE_TIMELINE: tuple[Beat, ...] = (
    Beat(
        at=0,
        page="統計概覽（首頁 /）",
        cue="開場。畫面停在統計概覽，不要動。",
        say="AutoPerf 用真實的 Android 手機跑效能測試。"
            "這頁是歷次測試累積的通過率跟趨勢。"
            "現在讓它從頭跑一次。",
    ),
    Beat(
        at=20,
        page="執行紀錄（/runs）→ 點最上面那一筆",
        cue="已排入 cold_start。切到執行紀錄，點最新的那一筆 run。",
        start=Run("cold_start", 12, key="smoke"),
        say="先從最快的。cold start 只做一件事："
            "開 YouTube，確認它到了前景。"
            "看手機——它自己亮了，是 adb 在下指令。"
            "右邊是手機的即時畫面，旁邊是同時量到的 CPU。",
    ),
    Beat(
        at=47,
        page="執行紀錄（/runs）→ 點最上面那一筆（新的 Golden run）",
        cue="已排入 play_golden。等幾秒讓它出現，點進去。",
        start=Run("play_golden", 30, key="golden"),
        say="接下來是影片。這一支直接用 deep link 開一支固定的影片，不搜尋。"
            "為什麼？同一個關鍵字搜三次，我量到兩支不同的影片，"
            "而三次都回報成功。",
    ),
    Beat(
        at=72,
        page="同一頁，往下看即時畫面與指標圖",
        cue="Golden 正在播。指著手機，再指畫面上的 HUD。",
        say="影片真的在播。但系統怎麼知道？"
            "不是看它在不在前景——點擊全打在空白處也一樣在前景，一樣綠燈。"
            "它讀 Android 的媒體工作階段，真的在播，那個值才會是 3。",
    ),
    Beat(
        at=98,
        page="長時間測試（/campaigns）→ 選最新的那一筆 repeat",
        cue="已排入 Groot（會排隊，等 Golden 跑完自動接上）。切到長時間測試頁。",
        start=Run("play_baby_groot_dancing", 30, key="groot"),
        say="下一支影片已經排進去了，會排隊，等這支跑完自動接上。"
            "趁空檔講壓力測試——單獨跑一次，再仔細也回答不了一件事："
            "這結果重現得了嗎？",
    ),
    Beat(
        at=120,
        page="同一頁，看「各情境穩定度」那一區",
        cue="指著 flaky 標籤與離散度數字。",
        say="同一台手機、同一個腳本、同樣秒數，連跑好幾輪，算兩件事："
            "指標在輪與輪之間差多少，還有這個 flaky 標籤——"
            "條件全固定，卻跑出不一樣的結果。",
    ),
    Beat(
        at=145,
        page="執行紀錄（/runs）→ 點 Groot 那一筆",
        cue="Groot 應該已經跑完。切回去看它的比較表。",
        wait_run="groot",
        say="回來看 Baby Groot，跑完了。這張表是跟基準線比，"
            "四項指標各差幾個百分比，超過門檻就判定退化。"
            "基準線分裝置、分腳本各自設定。",
    ),
    Beat(
        at=165,
        page="任務佇列（/queue）",
        cue="已排入 3 輪壓力測試，會繼續跑。收尾。",
        start=Campaign("repeat", "play_golden", 25, 3, key="closing"),
        say="最後再排三輪，它會自己跑完。"
            "真的手機、真的指標，每個數字都說得出它量的是不是那個畫面。謝謝。",
    ),
)

# 現場的總長。時間軸最後一段講完就到這裡。
LIVE_TOTAL_SECONDS = 180.0

# 中文口語速度，用來把講稿字數換算成秒數。抓保守一點：台上通常比排練慢。
CHARS_PER_SECOND = 3.3


# --- demo 前的預熱 ----------------------------------------------------------
#
# 現場那三分鐘要「看到結果」的東西，全部在這裡先產生：
#
#   1-3  三個 scenario 各跑一次。run 完成時如果那個「裝置 + 腳本」還沒有
#        基準線，worker 會自動把它設成基準線(dashboard/tasks.py)，所以現場
#        那兩支影片才有東西可以比。
#   4    壓力測試的歷史資料。四輪才看得出離散度，三輪以下講穩定度沒有說服力。
#   5-6  Q&A 備料。同一個腳本跑兩次，第二次刻意把 selector 拔掉，逼它掉回
#        寫死的座標。兩筆並排就是「selector 過期時會發生什麼事」的證據。
#        注意 dashboard 目前沒有顯示 selector_fallback 的畫面，要看得開
#        /api/runs/<id>/events —— 所以這是備料，不是流程的一部分。

PREWARM_STEPS: tuple[tuple[str, Run | Campaign], ...] = (
    ("建立 cold_start 的基準線", Run("cold_start", 12)),
    ("建立 play_golden 的基準線", Run("play_golden", 30)),
    ("建立 play_baby_groot_dancing 的基準線", Run("play_baby_groot_dancing", 30)),
    ("壓力測試資料：play_golden 重複 4 輪", Campaign("repeat", "play_golden", 25, 4)),
    ("Q&A 備料：selector 正常的對照組", Run("home_feed_tap_video", 25)),
    ("Q&A 備料：selector 失效(掉回座標)", Run("home_feed_tap_video", 25, blind=("home_feed_video",))),
)


# --- HTTP ------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


@dataclass
class Client:
    api: str
    dry_run: bool = False

    def get(self, path: str) -> object:
        return self._request("GET", path, None)

    def post(self, path: str, body: dict | None = None) -> object:
        if self.dry_run:
            return {"dry_run": True, "run_id": "dry-run", "campaign_id": "dry-run", "count": 0}
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict | None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.api}/api{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            raise ApiError(f"{method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path} → 連不上 {self.api}({exc.reason})") from exc


def start(client: Client, serial: str, what: Run | Campaign) -> dict:
    """把一個 Run/Campaign 排進去，回傳好認的識別資訊。

    `POST /api/runs` 回的鍵是 `run_id`，`POST /api/campaigns` 回的是
    `campaign_id` —— 兩邊不一樣，這裡各自取各自的。
    """
    if isinstance(what, Run):
        body: dict = {"serial": serial, "duration": what.duration,
                      "youtube_scenario": what.scenario}
        if what.blind:
            body["blind_targets"] = list(what.blind)
        result = client.post("/runs", body)
        run_id = result.get("run_id", "?") if isinstance(result, dict) else "?"
        return {"kind": "run", "id": run_id,
                "label": what.scenario + ("（selector 已拔除）" if what.blind else "")}
    result = client.post("/campaigns", {
        "kind": what.kind, "serial": serial, "duration": what.duration,
        "scenario": what.scenario, "iterations": what.iterations,
    })
    campaign_id = result.get("campaign_id", "?") if isinstance(result, dict) else "?"
    count = (result.get("count") if isinstance(result, dict) else None) or what.iterations
    return {"kind": "campaign", "id": campaign_id,
            "label": f"{what.kind} × {count} 輪 · {what.scenario}"}


# --- 輸出 ------------------------------------------------------------------

WIDTH = 76


def rule(char: str = "=") -> None:
    print(char * WIDTH)


# 可以斷行的地方。破折號刻意不在裡面 ——「——」是兩個字元，斷在中間會變成
# 一行結尾一個破折號、下一行開頭又一個。
BREAK_AFTER = "。，、；：？！"


def display_width(text: str) -> int:
    """終端機上佔幾欄。中文是兩欄，`len()` 會少算一半而讓每一行溢出。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def wrapped(text: str, indent: str = "  ") -> None:
    """依中文標點斷行。textwrap 對中文沒有用，它是照空白斷的。"""
    line = ""
    for char in text:
        line += char
        soft = char in BREAK_AFTER and display_width(indent + line) >= WIDTH - 32
        if soft or display_width(indent + line) >= WIDTH - 4:
            print(indent + line)
            line = ""
    if line.strip():
        print(indent + line)


def announce(index: int, total: int, beat: Beat, elapsed: float, started: dict | None) -> None:
    print()
    rule()
    print(f" [{index}/{total}]  {_mmss(beat.at)} → 目前 {_mmss(elapsed)}"
          f"{'   ⚠ 落後 %.0f 秒' % (elapsed - beat.at) if elapsed - beat.at > 8 else ''}")
    rule("-")
    print(f" 畫面   {beat.page}")
    print(f" 操作   {beat.cue}")
    if started:
        print(f" 已排入 {started['label']}  (id {started['id'][:8]})")
    rule("-")
    wrapped(beat.say, indent=" 　")
    rule()


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


# --- 等待 ------------------------------------------------------------------

def _enter_pressed() -> bool:
    """有沒有人按了 Enter。按了就跳到下一段，不用等時鐘。"""
    try:
        import msvcrt
    except ImportError:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            sys.stdin.readline()
            return True
        return False
    pressed = False
    while msvcrt.kbhit():
        msvcrt.getwch()
        pressed = True
    return pressed


def hold(seconds: float) -> None:
    """睡 `seconds`，但任何時候按 Enter 都會提早結束。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _enter_pressed():
            return
        time.sleep(0.2)


def wait_for_run(client: Client, run_id: str, timeout: float,
                 quiet: bool = False) -> dict | None:
    """等一個 run 進到終局狀態。回傳它，或逾時回 None。"""
    if client.dry_run or not run_id or run_id == "dry-run":
        return None
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}")
        status = run.get("status", "?") if isinstance(run, dict) else "?"
        if not quiet and status != last:
            print(f"    · {status}")
            last = status
        if status in ("completed", "failed", "interrupted"):
            return run if isinstance(run, dict) else None
        time.sleep(2)
    print(f"    · ⚠ 等了 {timeout:.0f} 秒還沒結束，先往下走")
    return None


def wait_for_campaign(client: Client, campaign_id: str, timeout: float) -> None:
    if client.dry_run or not campaign_id or campaign_id == "dry-run":
        return
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        detail = client.get(f"/campaigns/{campaign_id}")
        if not isinstance(detail, dict):
            return
        progress = f"{detail.get('finished_count', 0)}/{detail.get('run_count', 0)}"
        if progress != last:
            print(f"    · {progress} 完成")
            last = progress
        if detail.get("status") in ("completed", "failed", "interrupted"):
            return
        time.sleep(3)
    print(f"    · ⚠ 等了 {timeout:.0f} 秒還沒跑完，先往下走")


# --- 裝置 ------------------------------------------------------------------

def resolve_serial(client: Client, wanted: str | None) -> str:
    if client.dry_run:
        return wanted or "DRY-RUN-SERIAL"
    # 帶一個空的 JSON body，而不是完全沒有 body：一個沒有 Content-Length 的
    # POST 不是每一層都處理得一樣好，而這是整個流程的第一個請求。
    client.post("/devices/refresh", {})
    devices = client.get("/devices")
    if not isinstance(devices, list) or not devices:
        raise ApiError("dashboard 看不到任何裝置。先確認 adb devices 有東西，"
                       "手機是解鎖狀態，而且已經授權這台電腦。")
    if wanted:
        for device in devices:
            if device.get("serial") == wanted:
                return wanted
        raise ApiError(f"找不到序號 {wanted}。目前看得到的是："
                       + ", ".join(d.get("serial", "?") for d in devices))
    if len(devices) > 1:
        print("  ⚠ 連著不只一台裝置，用第一台。要指定請加 --serial")
    device = devices[0]
    print(f"  裝置  {device.get('nickname') or device.get('model') or '?'}"
          f"  ({device.get('serial')})  電量 {device.get('battery_level', '?')}%")
    return device["serial"]


# --- 指令：check ------------------------------------------------------------

def cmd_check(args, client: Client) -> int:
    """把 demo 會用到的每一個東西都戳一次，現在壞總比台上壞好。"""
    problems: list[str] = []

    def report(ok: bool, label: str, detail: str, fix: str = "") -> None:
        print(f"  {'✔' if ok else '✘'} {label:<22}{detail}")
        if not ok and fix:
            problems.append(f"{label}:{fix}")

    rule()
    print(" 前置檢查")
    rule()

    try:
        client.get("/stats")
        report(True, "Django API", args.api)
    except ApiError as exc:
        report(False, "Django API", str(exc),
               "python webapp\\manage.py runserver 8000")
        rule()
        print("\n API 都連不上，後面的檢查沒有意義。先把它開起來。")
        return 1

    try:
        queue = client.get("/queue")
        broker = bool(queue.get("broker_reachable")) if isinstance(queue, dict) else False
        worker = bool(queue.get("worker_online")) if isinstance(queue, dict) else False
        report(broker, "Redis broker", "可連線" if broker else "連不上",
               "docker start autoperf-redis")
        # solo worker 忙的時候答不出 inspect()，所以「沒回應」不必然是壞掉；
        # 但 demo 前這台手機應該是閒的，所以這時候沒回應就是真的沒開。
        report(worker, "Celery worker", "有回應" if worker else "沒有回應",
               "python scripts\\start-worker.py")
    except ApiError as exc:
        report(False, "任務佇列", str(exc), "檢查 Redis 與 worker")

    with socket.socket() as probe:
        probe.settimeout(1.5)
        alive = probe.connect_ex((LIVESCREEN_HOST, LIVESCREEN_PORT)) == 0
    report(alive, "即時畫面服務", f"port {LIVESCREEN_PORT}" if alive else "沒有開",
           "cd webapp && ..\\venv\\Scripts\\python.exe -m livescreen.server --port 8100")

    report(shutil.which("ffmpeg") is not None, "ffmpeg",
           "在 PATH 上" if shutil.which("ffmpeg") else "找不到（不影響 demo，只是不會有重播影片）")

    try:
        with urllib.request.urlopen(args.ui, timeout=3):
            report(True, "前端 (Vite)", args.ui)
    except Exception:
        report(False, "前端 (Vite)", f"連不上 {args.ui}", "cd webapp\\frontend && npm run dev")

    try:
        serial = resolve_serial(client, args.serial)
        report(True, "裝置", serial)
    except ApiError as exc:
        report(False, "裝置", str(exc), "adb devices / 解鎖手機 / 重新授權")
        serial = None

    if serial:
        print()
        print("  基準線（現場的比較表要靠它）")
        # adb-over-WiFi 的序號長成 192.168.1.50:5555，冒號要跳脫才不會被
        # 當成路徑的一部分。
        quoted = urllib.parse.quote(serial, safe="")
        for scenario in ("play_golden", "play_baby_groot_dancing", "cold_start"):
            try:
                client.get(f"/devices/{quoted}/baseline?scenario={scenario}")
                report(True, f"  {scenario}", "已設定")
            except ApiError:
                report(False, f"  {scenario}", "還沒有", "python scripts/demo.py prewarm")

    rule()
    if problems:
        print(f"\n 有 {len(problems)} 項要處理：")
        for problem in problems:
            print(f"   · {problem}")
        return 1
    print("\n 全部就緒。")
    return 0


# --- 指令：prewarm ----------------------------------------------------------

def cmd_prewarm(args, client: Client) -> int:
    serial = resolve_serial(client, args.serial)
    total = len(PREWARM_STEPS)
    started_at = time.monotonic()

    print()
    rule()
    print(f" 預熱 · {total} 個步驟 · 預估 5-6 分鐘")
    print(" 這段期間不要動手機，螢幕保持解鎖。")
    rule()

    for index, (label, what) in enumerate(PREWARM_STEPS, start=1):
        print(f"\n [{index}/{total}] {label}")
        try:
            info = start(client, serial, what)
        except ApiError as exc:
            print(f"    ✘ 排入失敗：{exc}")
            return 1
        print(f"    → {info['label']}  (id {info['id'][:8]})")
        if info["kind"] == "run":
            budget = what.duration + 90
            run = wait_for_run(client, info["id"], budget)
            if run:
                quality = run.get("quality") or {}
                print(f"    · 驗證 {'通過' if quality.get('verified') else '未通過'}"
                      f" · selector 掉回座標 {quality.get('selector_fallbacks', 0)} 次"
                      f" · 讀畫面 {quality.get('ui_introspections', 0)} 次")
        else:
            wait_for_campaign(client, info["id"], (what.duration + 45) * what.iterations + 60)

    print()
    rule()
    print(f" 預熱完成，花了 {_mmss(time.monotonic() - started_at)}。")
    print(" 接著跑 python scripts/demo.py check 確認基準線都在。")
    rule()
    return 0


# --- 指令：live -------------------------------------------------------------

def cmd_live(args, client: Client) -> int:
    serial = resolve_serial(client, args.serial)
    ids: dict[str, str] = {}
    total = len(LIVE_TIMELINE)

    print()
    rule()
    print(" 三分鐘 demo" + ("（排練模式，不會真的送出 run）" if args.dry_run else ""))
    print(f" 裝置 {serial}")
    print(" " + ("每一段按 Enter 進下一段。" if args.manual
                 else "照時間軸自動前進；想提早進下一段就按 Enter。"))
    print(f" 執行紀錄 {args.ui}/runs")
    rule()
    input("\n 準備好了按 Enter 開始 ")

    origin = time.monotonic()
    for index, beat in enumerate(LIVE_TIMELINE, start=1):
        if beat.wait_run and beat.wait_run in ids:
            run = client.get(f"/runs/{ids[beat.wait_run]}") if not client.dry_run else None
            if isinstance(run, dict) and run.get("status") in ("pending", "running"):
                print(f"\n    · 等 {beat.wait_run} 跑完…")
                wait_for_run(client, ids[beat.wait_run], 90, quiet=True)

        if args.manual:
            if index > 1:
                input("\n 按 Enter 進下一段 ")
        else:
            remaining = beat.at - (time.monotonic() - origin)
            if remaining > 0:
                hold(remaining)

        started = None
        if beat.start is not None:
            try:
                started = start(client, serial, beat.start)
                if beat.start.key:
                    ids[beat.start.key] = started["id"]
            except ApiError as exc:
                # 台上不要因為一個排隊失敗就停下來。印出來，繼續講。
                started = {"label": f"⚠ 排入失敗：{exc}", "id": "--------"}

        announce(index, total, beat, time.monotonic() - origin, started)

    print()
    rule()
    print(f" 結束，總長 {_mmss(time.monotonic() - origin)}。")
    if not args.dry_run:
        print(" 剛剛排的三輪壓力測試還在跑，可以留在任務佇列頁上。")
    rule()
    return 0


# --- 指令：budget ----------------------------------------------------------

def simulate_queue() -> dict[str, tuple[float, float]]:
    """每個 run 實際會在第幾秒開始與結束。

    solo worker 一次只跑一個 run，所以「排進去」不等於「開始跑」——一個 run
    要等前一個結束才輪得到它。整條時間軸最脆弱的假設就藏在這裡：Groot 在
    第 98 秒排進去，但它真正跑完是第幾秒，取決於 Golden 跑多久。改任何一個
    `duration` 都會動到它，而在台上才發現「切回去結果還沒好」已經太晚。
    """
    schedule: dict[str, tuple[float, float]] = {}
    free_at = 0.0
    for beat in LIVE_TIMELINE:
        if not isinstance(beat.start, Run):
            continue
        began = max(beat.at + ENQUEUE_OVERHEAD_HINT, free_at)
        ended = began + beat.start.duration + RUN_TEARDOWN_HINT
        free_at = ended
        if beat.start.key:
            schedule[beat.start.key] = (began, ended)
    return schedule


def _report_queue(client: Client) -> int:
    """印出佇列模擬，並檢查每個「等這個 run」的段落真的等得到。回傳問題數。"""
    schedule = simulate_queue()
    print()
    print(" 佇列模擬（solo worker 一次一個 run）")
    rule("-")
    for key, (began, ended) in schedule.items():
        print(f" {key:<8}{_mmss(began)} → {_mmss(ended)}")

    problems = 0
    for beat in LIVE_TIMELINE:
        if not beat.wait_run:
            continue
        window = schedule.get(beat.wait_run)
        if window is None:
            print(f"\n ⚠ 有一段要等 {beat.wait_run}，但時間軸上沒有這個 run。")
            problems += 1
            continue
        slack = beat.at - window[1]
        print(f" 切回 {beat.wait_run} 在 {_mmss(beat.at)}，它 {_mmss(window[1])} 跑完"
              f" → 餘裕 {slack:+.0f}s{'  ⚠ 會等到' if slack < 0 else ''}")
        if slack < 0:
            problems += 1
    rule()
    if problems:
        print(f"\n ⚠ 有 {problems} 處在切過去的時候 run 還沒跑完。腳本會自己等，"
              "但那幾秒你會站在台上沒有東西可以講。")
    return problems

def cmd_budget(args, client: Client) -> int:
    """講稿字數對不對得上時間軸。改完稿一定要跑一次。

    第一版的稿念完要 308 秒，塞在 180 秒的時間軸裡——每一段都超過十幾秒，
    而超時在台上不會有人提醒你，只會變成後面全部趕。所以這件事要用算的。
    """
    print()
    rule()
    print(f" 講稿配速 · 目標 {_mmss(LIVE_TOTAL_SECONDS)} · 以 {CHARS_PER_SECOND} 字/秒估算")
    rule()
    print(f" {'段':<4}{'起點':>6}{'窗口':>7}{'字數':>7}{'念完':>7}{'餘裕':>8}")
    rule("-")

    over = 0
    for index, beat in enumerate(LIVE_TIMELINE):
        nxt = (LIVE_TIMELINE[index + 1].at if index + 1 < len(LIVE_TIMELINE)
               else LIVE_TOTAL_SECONDS)
        window = nxt - beat.at
        spoken = len(beat.say) / CHARS_PER_SECOND
        slack = window - spoken
        if slack < 0:
            over += 1
        print(f" {index + 1:<4}{_mmss(beat.at):>6}{window:>6.0f}s{len(beat.say):>7}"
              f"{spoken:>6.0f}s{slack:>+7.0f}s{'  ⚠ 超時' if slack < 0 else ''}")

    total_chars = sum(len(beat.say) for beat in LIVE_TIMELINE)
    total_spoken = total_chars / CHARS_PER_SECOND
    rule("-")
    print(f" {'合計':<4}{'':>6}{LIVE_TOTAL_SECONDS:>6.0f}s{total_chars:>7}"
          f"{total_spoken:>6.0f}s{LIVE_TOTAL_SECONDS - total_spoken:>+7.0f}s")
    density = total_spoken / LIVE_TOTAL_SECONDS
    print(f" 講話密度 {density:.0%}（留白給手機亮起來、影片開始播的那幾秒）")
    rule()

    over += _report_queue(client)

    if over:
        print(f"\n ⚠ 有 {over} 段講稿比它的窗口長。台上不會有人提醒你，"
              "只會變成後面全部趕。")
        return 1
    if density > 0.95:
        print("\n ⚠ 密度超過 95%：整場幾乎沒有停頓。手機亮起來、影片開始播"
              "的那幾秒需要留白，不然台下只顧著聽，沒有人在看螢幕。")
        return 1
    return 0


# --- 進入點 ----------------------------------------------------------------

def _add_shared_options(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
    """共用旗標，root 與每個子指令都掛一份。

    argparse 預設只認 `demo.py --dry-run live` 這個順序，但台上不會有人記得
    旗標要放在子指令前面。兩邊都掛就兩種順序都通；子指令那份的 default 用
    SUPPRESS，沒給的時候才不會把 root 已經解析好的值蓋回去。
    """
    def default(value):
        return value if defaults else argparse.SUPPRESS

    parser.add_argument("--api", default=default(DEFAULT_API),
                        help=f"Django API(預設 {DEFAULT_API})")
    parser.add_argument("--ui", default=default(DEFAULT_UI),
                        help=f"前端網址(預設 {DEFAULT_UI})")
    parser.add_argument("--serial", default=default(None),
                        help="指定裝置序號，省略就用看到的第一台")
    parser.add_argument("--dry-run", action="store_true", default=default(False),
                        help="不真的送出任何 run")


def main(argv: list[str] | None = None) -> int:
    # Windows 主控台預設是 cp950，講稿裡的破折號跟符號會炸掉。要在 argparse
    # 印任何東西之前就換掉，否則 --help 自己就是亂碼。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    root = argparse.ArgumentParser(
        description="AutoPerf 三分鐘 demo 驅動腳本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="流程稿與講稿：docs/DEMO_SCRIPT_zh-TW.md",
    )
    _add_shared_options(root, defaults=True)
    commands = root.add_subparsers(dest="command", required=True)

    for name, help_text in (("check", "前置檢查"),
                            ("prewarm", "demo 前的預熱，約 5-6 分鐘"),
                            ("live", "現場那三分鐘"),
                            ("budget", "講稿字數對不對得上時間軸")):
        sub = commands.add_parser(name, help=help_text)
        _add_shared_options(sub, defaults=False)
        if name == "live":
            sub.add_argument("--manual", action="store_true",
                             help="改成按 Enter 才進下一段")

    args = root.parse_args(argv)
    if not hasattr(args, "manual"):
        args.manual = False

    client = Client(api=args.api.rstrip("/"), dry_run=args.dry_run)
    handlers = {"check": cmd_check, "prewarm": cmd_prewarm,
                "live": cmd_live, "budget": cmd_budget}
    try:
        return handlers[args.command](args, client)
    except ApiError as exc:
        print(f"\n✘ {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
