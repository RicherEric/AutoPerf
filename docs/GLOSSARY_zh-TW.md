# AutoPerf 名詞對照表

`DEVICE_TESTING_zh-TW.md` 與 `TEST_PLAN_zh-TW.md` 用到的每一個專有名詞。
右欄的「為什麼存在」是重點 —— 這個專案幾乎每個名詞都是為了擋掉一個特定的失效模式才被造出來的。

**§〇 是縮寫與業界通用語**（看不懂 `adb`、`ABC`、`DPAD` 先翻這裡）；**§一以後是這個專案自己造的名詞**。

---

## 〇、縮寫與外來語

| 縮寫 | 全名 | 在這個專案裡是什麼 |
|---|---|---|
| **adb** | Android Debug Bridge | Google 官方的裝置控制通道，走 USB 或 WiFi。這個專案唯一碰得到真機的東西 |
| **`adb shell`** | — | 在裝置上執行一行指令並收回 stdout。整個裝置層的**唯一出口**（見**縫隙 / seam**） |
| **ABC** | Abstract Base Class（抽象基底類別） | Python `abc` 模組：規定子類別「一定要實作哪些方法」。`Adapter` 與 `DeviceProfile` 都是 ABC |
| **API** | Application Programming Interface | 這裡指 webapp 給前端用的 HTTP 介面 |
| **CI** | Continuous Integration（持續整合） | 每次 push 自動跑測試。本專案用 GitHub Actions，Ubuntu 與 Windows 各一次，**runner 上沒有手機** |
| **CLI** | Command-Line Interface | `autoperf devices / run / preflight / campaign` |
| **UI** | User Interface | 這裡專指手機畫面上的元素 |
| **UI Automator** | — | Android 內建的自動化框架。`uiautomator dump` 把當下整棵畫面元素樹輸出成 XML |
| **accessibility tree**<br>（無障礙樹） | — | 系統提供給螢幕報讀軟體的畫面結構。selector 找得到東西全靠 app 有把元素放上這棵樹 —— **播放器覆疊與 TV 版 YouTube 沒有** |
| **XML** | eXtensible Markup Language | `uiautomator dump` 的輸出格式 |
| **dump** | — | 「把當下狀態整個倒出來」。本文件沒特別註明時，「dump」一律指 `uiautomator dump`（2611ms，其他讀取的 20 倍） |
| **`dumpsys`** | dump system service | Android 的系統狀態輸出指令：`cpuinfo` / `battery` / `window` / `media_session`，各約 105–147ms |
| **`getprop`** | get property | 讀裝置系統屬性。`getprop ro.build.characteristics` 回 `phone` 或 `tv` —— profile 就是這樣選的 |
| **`am start`** | activity manager start | 啟動 app 的指令。`am start -a VIEW -d <url>` 就是 **deep link**（115ms 返回） |
| **`monkey`** | — | 另一種啟動方式，模擬點桌面圖示（506ms）。實測比 deep link 慢又不確定 |
| **DPAD** | Directional Pad（方向鍵） | 電視遙控器的上下左右 + OK。TV adapter 用它走焦點，因為 Chromecast 沒有觸控 |
| **regex** | regular expression（正規表示式） | 從一大段文字裡抓出想要的欄位。**§一** 第一筆帳就是一條只認舊格式的 regex |
| **content-desc** | content description | 元素的無障礙描述文字。**最穩的 selector** —— 改掉它會弄壞螢幕報讀，所以 app 作者不會亂改 |
| **resource-id** | — | app 內部給元素的識別字串。比座標穩，但**沒有任何理由為外人保持不變** |
| **px²** | 平方像素 | `min_area` 的單位（例如 200,000 px²） |
| **flaky** | — | 「有時候過、有時候不過」的測試。**本專案的失效模式不是 flaky，是自信的綠燈** |
| **fixture** | — | 測試用的假資料。手寫的 fixture 與程式碼天生一致，所以永遠反駁不了它 |
| **Spy / Stub** | — | 測試替身的兩種角色：Spy 記「**送出了什麼**」，Stub 供「**回什麼**」。對應 `RecordingAdb` 與 `DeviceAdb` |
| **mock / `patch`** | — | Python `unittest.mock`：暫時把某個物件換成假的 |
| **`subprocess`** | — | Python 啟動外部程式的模組。整個 codebase **只有 `adb.py` 碰它** |
| **Mixin** | — | 只提供行為、不能單獨使用的類別，混進別的類別裡用（`ElementActionsMixin`） |
| **Django / Celery / Redis** | — | 網頁框架／背景任務執行器／任務佇列。**核心引擎完全不引用它們** |
| **run / step / scenario** | — | 三個基本單位：一次量測是一個 **run**，run 依時間表執行 **step**，那份時間表叫 **scenario** |
| **baseline / candidate** | — | 比較的兩端：當作基準的那次 run，與要跟它比的那次 |

---

## 一、架構骨架

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **縫隙 / seam** | `adb.shell(serial, command, timeout)` —— 整個裝置層唯一碰得到外界的函式 | 只要蓋住這一個點，377 個測試就能在沒有裝置的情況下跑完 |
| **Humble Object** | 把「決定」與「I/O」拆開的模式：難測的 I/O 縮到最薄，邏輯留在純函式裡 | `uiauto` 的 `resolve()` 可以在沒有裝置時被完整測試 |
| **注入（injection）** | 模組不自己建立 client，而是把 `adb` 當第一個參數收進來 | 讓 `CapturedAdb`（真機文字）能被直接塞進 `is_playing` |
| **`AdbClientProtocol`** | 一個 typing Protocol，規定「能被當成 adb 用」的最小介面 | 真 client 與三種測試替身可以互換，型別檢查仍成立 |
| **Adapter** | 抽象基底類別，定義 6 個原語：`launch_app` / `stop_app` / `tap` / `swipe` / `key_event` / `screen_size` | 新平台只要實作這 6 個，其餘動作是它們組出來的 |
| **`ElementActionsMixin`** | 用那 6 個原語 + 一次 UI dump 組出的高階動作：`tap_element` / `verify_*` | 保持 Adapter 抽象介面最小；高階行為只實作一次 |
| **`DeviceProfile`** | 抽象工廠：一次決定 adapter、collectors、selector 表、套件名 | 分開拿是「TV 被正確驅動、卻用手機的假設量測」的來源 |
| **`ui_is_introspectable`** | profile 上的旗標。手機 `True`、Android TV `False` | TV 不把 UI 公開到 accessibility tree，對它評分 selector 會產出整份都是假發現的報告 |
| **`PACKAGE_MAP`** | 平台專屬的套件名對照（例如 YouTube 手機版 → `.tv` 版） | TV 上啟動的其實是別的套件，拿 scenario 的套件名去比對會永遠 unverified |

---

## 二、找到畫面上的東西

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **`Target`** | 一個 UI 元素的完整描述：一串 `Selector` + 一個座標 fallback + 名字 | 22 個 Target 集中在 `scenarios/selectors.py` —— YouTube 改版時只改這一個檔案 |
| **`Selector`** | 一條尋找條件：`content_desc` / `resource_id` / `text` / `class_name` / `index` / `clickable` / `min_area` | 一個 Target 列多條，由穩定到脆弱依序嘗試 |
| **穩定度順序** | content-desc → resource-id → 結構 → 座標 | content-desc 是 app 作者對使用者的承諾；resource-id 是內部實作細節，沒有理由保持穩定 |
| **`fallback`（座標）** | 比例座標 `(0.5, 0.35)`，所有 selector 都沒中時用 | 用了不算失敗 —— 那正是 selector 出現之前的行為；但會被記錄下來 |
| **`Resolution`** | resolve 的結果：命中的點 + `strategy` + 第幾條 selector 命中 | 「怎麼找到的」跟「有沒有找到」一樣重要 |
| **`strategy`** | 這一次是靠什麼找到的：`content_desc` / `resource_id` / `structure` / `coordinates` | `coordinates` 是唯一會觸發警告的值 |
| **`min_area`** | selector 條件：節點面積下限（例如 200,000 px²） | 濾掉頭像、三點選單這類小的可點擊裝飾，否則它們會先被選中 |
| **`SHORT_LABEL_LENGTH = 2`** | 兩字以內的 label 改用**完全比對**，不做子字串比對 | 猜的 label「你」只有一個字，子字串比對在影片標題裡找到它，自信地解析到那支影片的選單鈕 |
| **`ALL_TARGETS`** | 模組內所有 `Target` 自動收集成的 tuple | preflight 用它逐個評分，新增 Target 不必記得去登記 |

---

## 三、量測與排程

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **`ScenarioStep`** | 一個排定時間的動作：`at`（第幾秒）+ `action` + `kwargs` | scenario 是一份時間表，不是一段程式 —— 可以被 preflight 重放而不量測 |
| **`ScenarioPreset`** | 具名的 scenario：`name` / `description` / `tier` / `build(screen)` | `build` 收螢幕尺寸，所以同一個 preset 在不同解析度上都成立 |
| **`REGISTRY`** | 24 個 preset 的名稱對照表 | app 與 CLI 的下拉選單、tier 掃描都從這裡長出來 |
| **tier** | preset 的分層：`smoke` / `functional` / `regression` | 見 `TEST_PLAN_zh-TW.md` —— 這一層正在重新擬定 |
| **`TestRunner`** | 編排者：排 collector 取樣、依 `step.at` 排 scenario、記事件、隔離故障 | 一個 step 壞掉不該讓整個 run 消失 |
| **`Collector`** | 一次量測讀取：`CpuCollector` / `MemoryCollector` / `BatteryCollector` | 每個各對應一條 dumpsys，成本已量測（約 105–118ms） |
| **`MetricSample`** | 一筆帶時間戳的量測值 | |
| **`BatchWriter`** | 把樣本與事件批次寫進 storage | 每 0.05s 一次的取樣不該每次都打一次 DB |
| **`deep link`** | `am start -a VIEW -d <watch url>` —— 直接跳到特定影片 | 115ms 返回、0.57s 到前景、**0 次 dump**，且每次播同一支影片 |
| **`NamedVideo`** | 釘死的影片 ID + 標題，每個自動長出一個 `play_<key>` preset | baseline 對 candidate 要比較的是 app 的變化，不是「這次剛好推薦了不同長度的影片」 |
| **`campaign`** | 一個測試計畫，展開成很多個普通 run。兩種：`soak`（單一長時間 run）／`repeat`（同一個 scenario 或整個 tier 跑 N 次） | 子 run 就是普通的 run row，所以 baseline、比較、錄影、刪除全部不必特例處理 |
| **iteration-major** | tier campaign 的排序方式：跑完整個 tier 一輪，再跑第二輪 | 中途中斷時每個 scenario 的樣本數一樣多；反過來排會前面幾個滿、後面幾個掛零 |

---

## 四、證明它真的發生了

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **`verify_foreground`** | 讀 `dumpsys window` 的 `mCurrentFocus`，確認 app 在前景。94ms | `am start` 只保證 intent 送出，不保證 app 可用 |
| **`verify_playing`** | 讀 `dumpsys media_session` 的 `PlaybackState == 3`，最多等 8s | 唯一能區分「真的進到影片」與「四次點擊打在空白處」 |
| **`verify_element_state`** | 讀節點自己的 `selected` / `checked`，拒絕座標 fallback | 唯一能證明「按鈕真的被按下」而非「按鈕被找到並點了」 |
| **三態** | 所有驗證回 `True` / `False` / `None`。讀不到是 `None`，不當失敗 | 但第三態必須稀少、大聲、可區分 —— 永不失敗的 `None` 等於沒驗證 |
| **`PlaybackState == 3`** | Android media session 的 `STATE_PLAYING` | 抓這個值的 regex 只認舊格式時，`is_playing` 在所有現代裝置回 `None` —— 系統裡最強的檢查靜默地什麼都沒驗證 |
| **`mCurrentFocus`** | `dumpsys window` 裡標示目前取得焦點的視窗那一行 | |
| **`VerificationError`** | 步驟無法被滿足時丟出的例外（`ElementNotFound` 是它的子類） | 不丟例外的話，runner 會記成「動作完成」，run 綠燈收場卻量了一個沒被碰過的畫面 |
| **等狀態，不要取樣** | 輪詢直到條件成立或逾時，而不是在固定時間點檢查一次 | 固定時間點的檢查在快網路通過、慢網路失敗 —— 直播還在緩衝就被判 not-playing |

---

## 五、run 會留下的事件

| 事件 | 意思 | 影響 |
|---|---|---|
| **`adapter_action`** | 步驟完成 | 每個步驟都會有一筆 |
| **`selector_fallback`** | 步驟成功了，但是靠座標 | **不是失敗** —— 是 selector 開始腐化的早期警告 |
| **`ui_introspection`** | 這個步驟花了 N 次 dump、共 X 秒 | 記錄工具自己的成本，因為它跟被量測的 CPU 是同一顆 |
| **`verification_failed`** | 驗證沒過 | **會讓整個 run 被標成 unverified** |
| **`adapter_error`** | 其他例外 | |
| **`unverified`** | `verification_failures > 0` 的 run | 意思是「這些數字量到的不是 scenario 描述的東西」—— 讓腐化的 selector 表現成缺資料，而不是表現成一個看起來很合理的結果 |

---

## 六、測試替身

| 名詞 | 類型 | 用在哪 |
|---|---|---|
| **`RecordingAdb`** | Spy —— 斷言**送出了什麼** | 測 adapter 原語組出來的指令字串 |
| **`DeviceAdb`** | Stub —— 供給**裝置回什麼**，同時記 dump 次數 | 測整條流程、測元素動作 |
| **`CapturedAdb`** | 重播**真機擷取的文字** | `test_captures.py` —— 全套件唯一對照真機輸出的一層 |
| **`allow_unknown=True`** | 顯式放寬：未列出的指令不 raise 而是回空字串 | 嚴格是預設。曾經有兩個同名 `FakeAdb`，一個 `KeyError` 一個靜默回 `""`，所以打錯指令會不會失敗取決於你在哪個檔案 |
| **`Waits.instant()`** | 把所有等待歸零、但保留重試**次數**的設定 | stub 裝置不會變，等待只買得到 wall-clock；但「重試幾次」是測試該看到的行為 |
| **fixture** | 測試用的假資料 | 手寫的 fixture 與程式碼「天生一致」，所以永遠不可能反駁它 |

---

## 七、Capture（擷取）

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **capture** | 從真機存下來的三份文字：`*.hierarchy.xml` / `*.window.txt` / `*.media_session.txt` | 沒人擷取的 fixture 就是猜測 |
| **provenance（來歷）** | `manifest.json` 記的：機型 / Android 版本 / app build / 時間 / 當下的 focus / 讀取跨度 | `focus` 欄位抓到了「把 launcher 存成 shorts」這件事 |
| **讀取跨度（span）** | 一份 capture 的第一次讀取到最後一次讀取之間的秒數 | capture 不是快照，是一段時間窗 —— hierarchy 要 2.6–11.7s，dumpsys 只要 120ms |
| **便宜的先讀** | 先讀 dumpsys（各約 120ms），最後才讀 hierarchy | 否則會存下「播放中的 hierarchy」配「`STOPPED` 的 media_session」 |
| **拒絕儲存** | parse 不出節點就不寫檔 | 截斷的 capture 毫無價值，而且看起來像證據 |

---

## 八、Preflight（起飛前檢查）

| 名詞 | 是什麼 | 為什麼存在 |
|---|---|---|
| **preflight** | 在真機上把 scenario 走一遍，只評分 selector、**不量測、不產生 run** | 否則發現一條 selector 錯了的唯一方法，是跑完一小時的測試再回頭讀事件 |
| **scoped preflight** | 只檢查某一個 scenario 用到的元素（`search_and_play` 是 3 個，整個涵蓋組是 42 個） | 完整涵蓋組要好幾分鐘，掛在一次 30 秒的 run 前面不成立；而 deep-link preset 一個元素都不解析，對它跑 preflight 會回一份「什麼都沒檢查」的綠燈報告 —— 所以那種情況是**拒絕執行**，不是照跑 |
| **裝置鎖** | 一台裝置同時只能被一個 run **或**一個 preflight 驅動（同一個 SQLite UPDATE 決定） | preflight 自己也在點、也在 dump。讓它跟量測 run 重疊，等於把工具自己的成本混進被量測的數字裡 |
| **`covering_scenarios()`** | 貪婪 set cover：用最少的 scenario 覆蓋到全部 22 個 target | 多數 preset 共用 `_enter_video_steps`，全跑會檢查 `search_icon` 十幾次卻沒增加任何覆蓋 |
| **`MATCHED`（selector）** | 靠 selector 找到 —— 這才叫「正常」 | |
| **`FALLBACK`（coordinates）** | 只靠座標找到 | 這是腐化 |
| **`MISSING`** | 完全找不到，也沒有座標可退 | |
| **`NOT_APPLICABLE`** | 這個平台不公開 UI，沒有 selector 可評分 | 跟 `FALLBACK` 分開，因為它不是腐化、也沒有東西可修 —— 混在一起會產出整份都是假發現的 TV 報告 |
| **`observed_on_screen`** | selector 沒中時，一併記下當下畫面上有什麼（最多 25 個） | 讓正確的 selector 可以直接從報告上讀出來，而不是再猜一次 |
| **`_reset()`** | 每個 scenario 之前強制停掉它會用到的 app | 不做的話狀態會外洩：上一個 scenario 留在觀看頁，下一個的 `launch_app` 只是把它切回前景，底部導航列理所當然不在，於是被誤判成 selector 腐化 |

---

## 九、預算與逾時（數字都是量測來的）

| 名詞 | 預設值 | 意義 |
|---|--:|---|
| **`resolve_budget`** | 7.0s | 一次 `tap_element` 找元素的**總**時間上限。真正的停止條件 |
| **`resolve_attempts`** | 3 | 次數**上限**，不是計畫 —— 因為一次嘗試的成本不是常數 |
| **`resolve_retry`** | 1.0s | 兩次 dump 之間的間隔 |
| **`dump_timeout`** | 6.0s | 單次 `uiautomator dump` 的逾時。照量到的最差值（11.66s）設計成「切掉」而不是「等完」 |
| **`adapter_action_timeout`** | 10.0s | runner 給單一動作的時間。**有一個測試斷言 `resolve_budget` 必須小於它** |
| **`state_timeout` / `state_poll`** | 4.0s / 0.5s | `verify_element_state` 的等待 |
| **`playback_timeout` / `playback_poll`** | 8.0s / 0.5s | `verify_playing` 的等待 |
| **`max_focus_steps`** | 12 | TV 上 DPAD 最多按幾次去搆一個目標 |
| **`resolve_budget_spent`** | — | 因為預算用完而掉到座標時記下的旗標，用來跟「這個 target 本來就沒有 selector」區分 |

> 為什麼要有預算：用中位數算，`3 次 dump × 3.0s + 2 次 retry × 1.0s = 11.0s > 10.0s`。
> 那個「為了救『步驟比 app 畫完更早到』而存在的重試」，實際上是讓整個步驟被殺掉 —— **比不重試更糟**。
