# 專案結構

> 量測基準：`wc -l` 實際計算，2026-08-01
> 這份文件回答一個問題：**打開這個 repo，東西在哪裡，以及為什麼在那裡。**

---

## 一、一句話版本

**核心引擎完全不知道有網頁存在。**

```
webapp  ──引用──▶  src/autoperf        19 處
webapp  ◀─引用──   src/autoperf         0 處
```

這兩個數字是可以驗證的：

```powershell
# webapp 引用 autoperf：19
Select-String -Path webapp\*.py,webapp\*\*.py -Pattern "from autoperf|import autoperf"

# autoperf 引用 django / celery / webapp：0
Select-String -Path src\autoperf\*.py -Pattern "django|celery|webapp"
```

**這個方向是單向的，而且是整個結構最重要的一件事。** 它換來三件具體的東西：

1. CLI 跟 dashboard 可以同時跑，因為它們是同一個引擎的兩個前端。
2. 417 個核心測試不需要 Django、不需要 Redis、不需要 Celery 就能跑完。
3. 哪天 dashboard 整個重寫，`src/autoperf` 一行都不用動。

---

## 二、目錄

```
AutoPerf/
├── src/autoperf/          核心引擎 · 5157 行 · 不知道有網頁
│   ├── adb.py             141   唯一的出口：adb.shell(serial, cmd, timeout)
│   ├── adapters.py        644   原語：點擊、滑動、按鍵、開關 app（手機 / TV 兩種實作）
│   ├── uiauto.py          461   決策：解析畫面結構、比對 selector、verify_*
│   ├── profiles.py        147   一次決定一整組零件（adapter + collector + target）
│   ├── collectors.py      101   量什麼：CPU、記憶體、電量、溫度
│   ├── runner.py          336   一次 run 的編排：採樣迴圈 + 場景步驟
│   ├── storage.py         667   唯一的資料落地點（SQLite，WAL）
│   ├── campaigns.py       416   長時間測試：soak（漂移）與 repeat（穩定度）
│   ├── analyzer.py        228   基準線比較、趨勢擬合
│   ├── workers.py          96   多裝置：一台一個 process + 心跳
│   ├── preflight.py       396   ★ 維護工具：走一遍場景，替每個 selector 評分
│   ├── capture.py         210   ★ 維護工具：把真機的輸出錄成 fixture
│   ├── screen_stream.py    79   H.264 串流的切分
│   ├── demo.py             60   刻意讓 selector 失效（示範與診斷用）
│   ├── models.py           47   資料型別與狀態列舉
│   ├── cli.py             464   指令列進入點
│   └── scenarios/               「要做什麼」——與「怎麼做」分開
│       ├── youtube.py     360   24 個測試場景（分 smoke / functional / regression）
│       ├── selectors.py   276   22 個 UI 元素，每個帶一條 selector 鏈
│       └── coords.py       25   比例座標（0.5, 0.8 → 實際像素）
│
├── webapp/                網頁層 · 讀同一個 SQLite
│   ├── dashboard/         Django API（services 484 / views 483 / tasks 66）
│   ├── config/            Django 設定與路由
│   ├── livescreen/        即時畫面：獨立的 asyncio 行程（server 281），不碰 SQLite
│   └── frontend/src/      Vue · 3265 行 · 8 個頁面 + 9 個元件
│
├── tests/                 22 個核心測試模組 · 417 個測試
│   └── captures/          ★ 真機錄下來的輸出 —— 全套唯一對照真實裝置的東西
│       └── sm_a5560_android15/
│           ├── *.hierarchy.xml      畫面結構
│           ├── *.media_session.txt  播放狀態
│           ├── *.window.txt         前景視窗
│           └── manifest.json        provenance：機型 / Android / app build / 時間
│
├── scripts/
│   ├── run-tests.py       跑測試，可以只跑一組（見下方「測試怎麼分組」）
│   ├── StartServices.py   一個指令依序開起整個 dashboard（Redis → API →
│   │                      worker → livescreen → 前端），每個都健康檢查
│   ├── start-worker.py    啟動 Celery worker，自動決定要開幾個
│   └── demo.py            三分鐘現場 demo 的驅動腳本
│
└── docs/                  繁體中文的設計文件（見文末清單）
```

★ 標記的三個東西——`preflight.py`、`capture.py`、`tests/captures/`——是**維護迴路**，
不是測試的一部分。它們存在的理由見 §四。

---

## 三、三個分層，一個縫隙

`src/autoperf` 內部不是平的，它是分層的，而且**只有一個地方會碰到裝置**：

```
編排層    runner.py · campaigns.py · workers.py
             │  「什麼時候做什麼」
決策層    uiauto.py · profiles.py · scenarios/
             │  「要點哪個東西，怎麼確認點到了」
Adapter   adapters.py
             │  「點擊 = input tap x y」（TV 上是走焦點）
─────────────┼──────────────────────────  唯一的縫隙
傳輸        adb.py  →  adb.shell(serial, cmd, timeout)
             │
實體裝置    Galaxy A55 / Chromecast
```

**決策層從不自己建立連線。** `uiauto` 裡每一個會發指令的函式，第一個參數永遠
是外面傳進來的 adb，模組自己從來不去拿一個。

這一個決定換來的是：測試只要換掉那一個參數，就能在**沒有裝置**的情況下驗證
任何一層。417 個核心測試全部靠這件事。

---

## 四、維護迴路在結構裡的位置

`preflight.py`、`capture.py` 跟 `tests/captures/` 常被誤認成測試工具，
它們不是——**它們是用來回答「selector 表還對不對」的**：

```
YouTube 改版
    │
    ├─▶ tests/captures/ 對不上   →  parsing + elements 兩組測試紅（114 個）
    │                                （另外 131 個 logic 完全不受影響）
    ├─▶ preflight.py             →  逐個 target 評分，miss 時列出畫面上實際有什麼
    │                                報告按 target 聚合 = 一張工作清單
    ├─▶ 修 scenarios/selectors.py   一個壞掉的 selector 只在這裡修一次
    │
    └─▶ capture.py 重錄          →  新的證據，manifest 記下是哪一版
                                      （錄製必須用 selector 導航，所以回路咬住自己的尾巴）
```

**這條回路有一個洞，寫在這裡免得被忘記**：`preflight` 至今是「要有人記得去跑」
的指令，不是一道關卡——因為這個專案沒有 CI，也沒有東西在真機上定時跑。

---

## 五、測試怎麼分組

測試**不是按檔案分的，是按「什麼情況下會壞」分的**：

| 組 | 什麼時候會壞 | 測試數 |
|---|---|--:|
| `logic` | 我們自己的邏輯改了。**這裡不知道「裝置」是什麼** | 131 |
| `parsing` | 裝置吐的東西跟 fixture 假設的不一樣 ← fixture 都在這 | 70 |
| `commands` | 送出去的指令形狀改了 | 44 |
| `elements` | selector 解析或 `verify_*` 改了 | 44 |
| `flow` | 整條流程對假裝置跑不通 | 128 |
| `webapp` | API / Celery / 串流 | 149 |
| | | **566** |

```powershell
python scripts\run-tests.py            # 全部
python scripts\run-tests.py --list     # 有哪些組、各自何時會壞
python scripts\run-tests.py parsing    # 只跑會過期的那一片
```

按模組分只是把 `src` 目錄抄一遍。**按失敗原因分，才回答得出維護者真正會問的
問題：「YouTube 改版了，我要看哪裡？」** ——答案是 `parsing` 跟 `elements`，
114 個，不是 566 個。

而且這張地圖不會爛掉：測試模組沒有歸組，「跑全部」會靜默跳過它，於是
`test_suite_groups.py` 失敗。**靜默跳過就是這整個專案在對抗的東西**，包括在
它自己的測試套件裡。

---

## 六、資料往哪裡流

```
Dashboard 按下執行
      │
      ▼
  Redis 佇列  ──▶  Celery worker  ──▶  TestRunner  ──▶  adb  ──▶  真機
                                            │
                                            ▼
                                    SQLite（WAL）  ← 唯一的落地點
                                            │
              ┌─────────────────────────────┤
              ▼                             ▼
        CLI 直接讀                  Django 直接查
                                （不經過 Redis，也不經過 Celery）
```

**讀取刻意不走佇列。** 如果讀取也排隊，使用者看報表就要等 worker 有空——但
報表跟 worker 忙不忙一點關係都沒有。WAL 模式允許「一個 writer + 多個 reader」，
正好是這個系統的形狀。

即時畫面（`webapp/livescreen/`）是**唯一不碰 SQLite 的東西**：它只是把
`adb exec-out screenrecord` 的輸出接到 WebSocket 上，所以它自己是一個獨立的
asyncio 行程，跟 Django 無關。

---

## 相關文件

| 文件 | 內容 |
|---|---|
| `docs/DEVICE_TESTING_zh-TW.md` | 裝置測試的完整架構與設計哲學（含流程圖） |
| `docs/PRESENTATION_NOTES_zh-TW.md` | 期末報告簡報的逐頁講稿 |
| `docs/DEMO_SCRIPT_zh-TW.md` | 三分鐘現場 demo 的流程稿 |
| `docs/TEST_PLAN_zh-TW.md` | 24 個場景怎麼分層 |
| `docs/UI_AUTOMATOR.md` | selector 策略的實測紀錄 |
| `docs/LIVE_SCREEN.md` | 即時畫面的裝置差異與 fallback |
| `docs/GLOSSARY_zh-TW.md` | 名詞對照 |
| `docs/CODE_REVIEW_zh-TW.md` | 目前已知缺什麼 |
