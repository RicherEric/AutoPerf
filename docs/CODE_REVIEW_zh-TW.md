# AutoPerf 程式碼審查：缺什麼

審查範圍：`src/autoperf`、`webapp`、`scripts`、`tests`（含目前未 commit 的工作），約 6,200 行 Python 加上 Vue 前端。
基準：`master` @ `a01a4ca` 加上工作目錄中的變更。
執行結果：核心 377 個測試全過；webapp 126 個測試 **2 個失敗**。

分成六類，A 是現在就壞的，B 是這個專案最在意的那種洞。

---

## A · 現在就是壞的

### A1 · 兩個 webapp 測試失敗

`webapp/dashboard/tests/test_enqueue.py:22` 與 `:29`

`trigger_run` 加了 `blind_targets` 參數之後，`apply_async` 的 args 多了一個元素，但 `test_enqueue.py` 的兩個斷言沒跟著改。`test_runs.py` 改了，`test_enqueue.py` 漏了。

```
Expected: apply_async(args=[..., 30, run_id, None],       task_id=run_id)
Actual:   apply_async(args=[..., 30, run_id, None, None], task_id=run_id)
```

改法：兩處預期值各補一個 `None`。

> 這件事本身很小，但它說明一件事：**沒有任何東西會在你 commit 之前跑這個套件**。見 D1。

### A2 · `demo.py` 的承諾沒有兌現

`src/autoperf/demo.py:17-19` 的 docstring 寫著：

> 「…**the run records which ones it blinded**, so a blinded run can never be mistaken for a genuine finding.」

程式碼沒有做這件事。`webapp/dashboard/tasks.py:55` 只是把 scenario 換掉：

```python
scenario = demo.blind_targets(scenario, blind_targets)
```

`create_run`（`storage.py:167`）沒有欄位存它，`TestRunner` 也沒有收到它，所以沒有任何事件被寫下。結果是：**一個刻意致盲的 run 產生的 `selector_fallback` 事件，跟一個真的 selector 腐化產生的完全一樣。**

這正是這個專案自己最在意的失敗模式 —— 看起來像證據的東西其實不是。而且它會污染歷史：三個月後回頭看事件記錄，沒有辦法區分「那天 YouTube 改版了」跟「那天我在示範」。

改法（二選一，第一個比較好）：

1. 在 `tasks.py` 呼叫 `blind_targets` 之後補一筆事件。這需要讓 `TestRunner` 接受預先要寫入的事件，或在 `run()` 前用一個獨立的 `BatchWriter` 寫。
2. 最小改動：`create_run` 加一個 `blinded_targets TEXT` 欄位（`initialize()` 已經有 ALTER TABLE 的模式可以照抄，見 `storage.py:82`），run 列表與 run detail 一併回傳。

無論哪一種，`run_quality` 都應該把「這個 run 被致盲過」當成一個會影響判讀的旗標。

---

## B · 量測可信度的洞

這三個是我認為最值得處理的。它們跟 `DEVICE_TESTING_zh-TW.md` §十 的原則 1（「不知道」不等於「失敗」，反面才致命）直接衝突 —— 那份文件把這個原則用在 scenario 驗證上，但**指標採集這一側還沒有套用同一條原則**。

### B1 · collector 的錯誤事件只寫不讀

`src/autoperf/runner.py:238, 241, 259, 215` 會寫出四種事件：

- `collector_error`
- `collector_timeout`
- `adapter_timeout`
- `app_version_unavailable`

全專案搜尋這四個字串，`runner.py` 以外只出現在兩個測試檔（`tests/test_core.py:72`、`tests/test_runner.py:123`）。**沒有任何 production 程式碼讀它們** —— 不在 `run_quality`、不在 `compare`、不在 API、不在 UI。

具體後果：如果 `BatteryCollector` 因為裝置回了不能解析的東西而每次採樣都拋例外，這個 run 會

1. 正常結束，狀態 `completed`；
2. `run_quality` 回 `verified: true`（它只數 `verification_failed` / `selector_fallback` / `ui_introspection`，見 `storage.py:321`）；
3. 在 Dashboard 上跟一個健康的 run 長得一模一樣；
4. 只是**沒有電量與溫度的資料**。

改法：`run_quality` 與 `run_quality_many` 的 kinds 清單加入 `collector_error`、`collector_timeout`，並且回傳一個 `metrics_incomplete` 之類的旗標。這兩個函式已經是現成的位置，改動很小。

### B2 · `compare()` 靜默跳過缺席的指標

`src/autoperf/analyzer.py:212-214`

```python
candidate_stats = candidate.get(name)
if candidate_stats is None:
    continue          # <-- 整場沒量到的指標，在這裡消失
```

baseline 有 `battery.level`、candidate 完全沒有 → 這個指標不會出現在比較結果裡，也不會有任何地方說「少了一個」。使用者看到的是一份「沒有退步」的報告。

跟 B1 合起來看，這是一條完整的沉默路徑：collector 全程失敗 → 事件沒人讀 → 指標缺席 → 比較跳過 → 綠燈。

改法：缺席時仍然產生一筆 `MetricComparison`，`candidate_mean=None`、`delta_pct=None`，並加一個 `missing=True`。呼叫端（CLI 與 Dashboard）把它顯示成「未量測」，跟「沒有退步」清楚分開。這比在 `compare` 裡直接判 `regressed=True` 好，因為缺席的原因不一定是退步 —— 例如 TV 本來就沒有電池。

### B3 · baseline 自動晉升沒有品質閘門

`webapp/dashboard/tasks.py:59-64`

```python
if (completed and completed["status"] == "completed"
        and storage.get_baseline(serial, youtube_scenario) is None):
    storage.set_baseline(serial, run_id)
```

唯一的條件是 `status == "completed"`。而 `run_quality` 已經知道怎麼判斷一個 run 值不值得信任 —— 這裡沒有問它。

所以以下每一種 run 都可以成為之後所有比較的基準：

- `verification_failed` 的 run（scenario 沒真的走到，量的是首頁）
- 被 `blind_targets` 致盲的示範 run（A2）
- collector 全程失敗、指標殘缺的 run（B1）

而且 baseline 一旦寫進去，後面每一次比較都以它為準 —— 一個壞掉的第一次會安靜地污染這台裝置這個場景的所有歷史。

`set_baseline`（`storage.py:581`）本身也沒有守門，`views.py:346` 讓使用者手動指定任何 run_id。手動的那條路留著沒問題（使用者是刻意的），但**自動晉升**這條應該要有條件。

改法：`tasks.py` 的條件加上 `storage.run_quality(run_id)["verification_failures"] == 0`。手動設定則在 API 回應裡附上該 run 的 quality，讓前端可以警告。

---

## C · 半成品

### C1 · 兩個新 API 前端沒有接

`webapp/dashboard/urls.py` 新增了：

- `runs/<run_id>/events` → `views.run_events`
- `selector-targets` → `views.selector_targets`

比對前端實際呼叫的路徑（`webapp/frontend/src` 全域搜尋），**這兩個都沒有被呼叫**，其他 22 個端點都有。

也就是說：`list_run_events`（`storage.py:239`）解決的那個問題 —— 「一個 run 記下的證據寫進資料庫之後，除了直接開 SQLite 以外沒有地方讀得到」—— 在 API 層解決了，但在使用者看得到的地方還沒有。`blind_targets` 同理：後端完整、有測試，但 UI 上沒有入口。

考慮到 `demo.py` 存在的目的就是**現場示範**，這一段沒接完等於功能還不能用。如果期末報告要現場 demo「selector 腐化長什麼樣子」，這是必須先補的。

### C2 · `blind_targets` 只走 webapp 這條路

`autoperf.demo` 只被 `webapp/dashboard/tasks.py` 呼叫。CLI（`cli.py`）與 campaign（`campaigns.py`）都沒有這個選項。CLI 那條路是最適合現場示範的（不用開 worker、不用開瀏覽器），反而沒有。

---

## D · 工程基礎建設（整類都不存在）

### D1 · 沒有 CI

沒有 `.github/`，沒有任何 CI 設定。503 個測試、一份設計良好的分組執行器（`scripts/run-tests.py`），但**沒有任何東西會自動跑它們**。A1 那兩個失敗的測試就是直接後果 —— 它們已經壞在工作目錄裡，只有在有人手動跑的時候才會知道。

以這個專案的形狀，一個最小的 workflow 就夠：push 時跑 `python scripts/run-tests.py`。核心套件 25 秒、webapp 7 秒，不需要任何裝置。**「368 個測試不需要真機」這個賣點，本來就是為了讓 CI 可行而存在的設計** —— 但 CI 沒有被建起來，這個設計的價值有一半沒有兌現。

### D2 · 沒有 linter / formatter

`pyproject.toml` 裡沒有 ruff、flake8、black 或任何等價設定。程式碼風格其實相當一致（顯然是人為維持的），但那是靠紀律而不是靠工具 —— 而這個專案自己的原則 4 就是「**用結構取代紀律**」（`DEVICE_TESTING_zh-TW.md` §十）。

`ruff` 一個工具就能同時做 lint 與 format，設定約 10 行。

### D3 · 沒有型別檢查

整個 codebase 幾乎全面標註型別，用了 `Protocol`、`dataclass(slots=True)`、`X | None`。但沒有 mypy 或 pyright 設定，所以**這些標註沒有任何東西在檢查**。

這裡的投資報酬率特別高，因為 `AdbClientProtocol` 這種 structural typing 的正確性正是型別檢查器最擅長驗證的東西 —— 現在如果某個測試替身的 `shell` 簽章跟 Protocol 對不上，要到執行時才會知道。

### D4 · 沒有覆蓋率量測

沒辦法回答「哪一層測得薄」。以這個專案對測試分組的講究程度，缺這個有點意外。`coverage` 加進 `run-tests.py` 是小改動。

### D5 · 前端零測試

`webapp/frontend/package.json` 只有 `dev` / `build` / `preview`，沒有測試框架、沒有測試檔。Vue 前端包含 API 呼叫、i18n（多語系）、以及即時串流的 canvas 解碼邏輯（`WebCodecs`），全部沒有自動化驗證。

考慮到後端測試如此完整，這個落差很明顯。至少 `api.js` 與 composables 值得補 vitest。

---

## E · 部署與安全

### E1 · Dashboard 沒有任何認證，而使用情境是教室 WiFi

- `webapp/config/settings.py:24` — SECRET_KEY 硬寫在原始碼裡
- `webapp/config/settings.py:27` — `DEBUG = True` 硬寫
- `webapp/config/settings.py:29` — `ALLOWED_HOSTS = []`
- `views.py` 每一個 POST 端點都是 `@csrf_exempt`，而且沒有 `INSTALLED_APPS` 裡的 auth

目前 `DEBUG=True` 加空的 `ALLOWED_HOSTS` 讓 Django 只接受 localhost，所以實際暴露面有限。**但這是唯一擋著的東西** —— 任何人為了讓別台電腦看 Dashboard 而去改 `ALLOWED_HOSTS`（這正是 `adb connect` 那套教室情境會做的事），就會同時打開一個無認證的控制面。

那個控制面能做什麼：`views.device_control`（`views.py:48`）可以對任何已連線的裝置送任意座標的 tap 與按鍵；`trigger_run` 可以佔用裝置；`delete_recording` 可以刪檔案。

不需要做成產品級。但至少：

1. `SECRET_KEY` 與 `DEBUG` 改讀環境變數（`os.environ.get`，程式碼已經 import 了 `os`）；
2. `INSTALL.md` 裡明確寫「這個服務只能綁 localhost；要跨機器存取請自己加反向代理與認證」。

### E2 · 沒有資料保留策略

`metric_samples` 與 `test_events` 只增不減。一次 60 秒的 run，CPU 每秒 3 筆，加上事件，長期跑下來 SQLite 會單調成長，沒有任何清理或封存路徑。soak campaign（設計上就是要跑很久）會加速這件事。

---

## F · 文件與現實不同步

這幾條之所以要列，是因為它們都出現在你要拿去報告的文件裡。

| 位置 | 寫的 | 實際 |
|---|---|---|
| `DEVICE_TESTING_zh-TW.md` §二 | 368 個測試 | 核心 377（+`test_demo` 9 個） |
| `DEVICE_TESTING_zh-TW.md` §四 表格 | logic 109 / webapp 111 | logic 118 / webapp 126 |
| `TEST_PLAN_zh-TW.md` | scenario 分五層 | 程式碼仍是三層（`youtube.py:18-20`）；這份是規劃 |
| 期中簡報 | 19 個 scenario | 24 個（smoke 5 / functional 8 / regression 11） |

`TEST_PLAN` 那一條最需要注意 —— 它讀起來像是已完成的設計，但 `youtube.py` 的 `TIERS` 還是 `smoke` / `functional` / `regression`。文件裡應該標一行「狀態：規劃中，尚未實作」。

---

## 建議順序

如果只做三件事：

1. **A1**（補兩個 `None`）+ **D1**（一個 CI workflow）。加起來不到一小時，而且第二件會讓第一件不再重演。
2. **B1 + B2 + B3**。這三個是同一條沉默路徑的三段，一起修才有意義，而且它們是這個專案唯一與自己的核心主張相牴觸的地方。
3. **C1**。如果期末報告要現場 demo selector 腐化，這是前提。

D2 / D3（ruff + mypy）建議在 CI 建起來之後再加，因為第一次跑一定會有一批既有的告警要處理，跟修 bug 混在一起會很難 review。

---

## 沒有問題的部分

為了公平，也記錄一下我特地去找但沒找到問題的地方：

- **指令注入**：`adb.py` 全程用 argv list、不用 `shell=True`，`serial` 有 `re.fullmatch` 白名單，`connect` / `pair` 的位址與配對碼都有格式驗證。
- **SQL 注入**：`storage.py` 所有查詢都是參數化的；動態組出來的只有 `?` 佔位符的數量。
- **並行正確性**：`BatchWriter` 的 sentinel + `join()`、`daemon=False`、有上限的 queue、`try_start_run` 的原子性宣告，都經得起看。
- **例外處理**：全 codebase 沒有一個裸 `except:`；`runner.py` 每一條例外路徑都轉成事件而不是吞掉。
- **測試分組的完整性**：`test_suite_groups` 會驗證每個測試模組都被歸到某一組，所以新增測試檔不會安靜地不被執行。
