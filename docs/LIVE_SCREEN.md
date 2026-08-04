# Live Screen：手機與 Android TV 處理方式

AutoPerf 的 Live Screen 會依裝置類型選擇不同的傳輸策略。手機重視畫面
流暢度；Chromecast／Google TV／Android TV 則優先確保能快速取得畫面、
穩定監控及持續自動重連。

## 共用連線流程

前端的 `useDeviceScreen()` 在連線前會向 `GET /api/devices` 取得裝置資料，
再以 `model`、`product`、`manufacturer`、`brand`、`device` 和 `nickname`
判斷裝置類型。目前可辨識的 TV 關鍵字包含：

- `Chromecast`
- `Google TV`、`Android TV`、`Smart TV`
- `sabrina`、`BRAVIA`、`SHIELD`

Live Screen WebSocket 位址預設為 `ws://127.0.0.1:8100/stream/<serial>`。
同一個裝置只允許一條畫面串流；新的連線會先等待舊串流完成清理。不同
裝置則可同時監控，供 Mission Control 多裝置測試使用。

所有模式在非人工中斷時都會自動重連，間隔依序為 1、2、5、10 秒，之後
維持每 10 秒重試。使用者按下中斷或離開元件後才會停止重試。

## 手機

手機預設使用 H.264：

1. 伺服器執行
   `adb exec-out screenrecord --output-format=h264 --time-limit=0 -`。
2. 伺服器將 Annex-B NAL 組成 access unit，透過 WebSocket 傳送。
3. Chrome／Edge 使用 WebCodecs `VideoDecoder` 即時解碼。
4. 第一個可解碼影格的等待上限為 20 秒。

下列情況會切換成 screenshot fallback：

- 瀏覽器沒有 WebCodecs。
- H.264 在 20 秒內沒有首幀。
- WebSocket、編碼器或解碼器發生錯誤。

手機 fallback 每 0.7 秒擷取一次畫面，送出前等比例縮至最高 720 px 寬，
並轉成 JPEG。若 ffmpeg 不存在，伺服器會傳送原始 PNG，監控仍可運作，
但資料量會較大。

H.264 模式可在 Run Detail／Mission Control 開啟畫面時同步 remux 為 MP4
測試錄影。錄影依賴 ffmpeg；screenshot fallback 不產生 MP4。

### 靜止的畫面：為什麼首幀需要 idle flush

`screenrecord` **只在畫面有變化時才輸出影格**。Annex-B 沒有長度欄位，一個
NAL 的結尾是「下一個 start code 的開頭」——所以停在啟動器不動的裝置，第一個
IDR 會被押在切分器裡，等一個不會發生的第二張影格。**畫面永遠是黑的，而那個
畫面存在的目的正是讓人去把裝置弄動。**

2026-08-03 在 Redmi Pad 2（b84fee70）實測：透過 WebSocket 12 秒 **0 影格**；
同一份程式對 Galaxy A55 正常，只因為手機畫面一直在動。

伺服器因此改成有逾時的讀取：串流靜默滿 `IDLE_FLUSH_SECONDS`（1 秒）就呼叫
`AnnexBSplitter.flush()`，把押著的 NAL 當成完整影格送出。一秒遠長於一張影格
寫進 pipe 的間隔，所以這時候押著的東西是「寫完了」而不是「寫到一半」。

修正後同一台 Redmi Pad 2 立刻拿到 KEY 影格（44480 bytes）。

**連帶修好的是點擊。** `DeviceScreenView.onCanvasClick` 在 canvas 還沒有尺寸
時直接 return，而 canvas 的尺寸來自第一張影格——所以沒有影格就等於整個畫面
點不動。方向鍵與 Home／Back 按鈕不經過 canvas，當時是正常的。

### 裝置自己回報的失敗，會出現在 stdout 裡

`screenrecord` 把自己的錯誤印在 **stdout**（不是 stderr），排在位元流前面，
然後降解析度繼續跑。Redmi Pad 2 的實際輸出：

```
ERROR: unable to configure video/avc codec at 2048x1280 (err=-22)
WARNING: failed at 2048x1280, retrying at 1280x720
```

切分器會把這段文字當成「不是 start code」跳過——正確，但**安靜**，而它是
「為什麼這台裝置的預覽不是它的真實解析度」唯一的解釋。伺服器現在會把第一個
chunk 裡位元流之前的文字記進 log（`screenrecord said: ...`）。

## Chromecast／Google TV／Android TV

TV 不先嘗試 H.264，而是直接進入 screenshot 模式。實機上 TV 的
`screenrecord` 啟動及 IDR 首幀通常比手機慢；若先等待 H.264 再 fallback，
每次連線很容易先撞到 20 秒 timeout。

TV screenshot 策略如下：

- 立即執行 `adb exec-out screencap -p`，不等待 H.264。
- 每 1.2 秒更新一次，適合確認頁面切換、影片播放狀態及遙控器操作結果。
- 原始 1920×1080 PNG 在伺服器端、送進 WebSocket 前等比例縮成最高
  960 px 寬；16:9 TV 通常輸出 960×540 JPEG。
- 縮圖維持原始長寬比，不會拉伸或壓扁畫面。
- 斷線後仍以 TV screenshot 模式重連，不會重新進入 H.264 等待。

Chromecast 實測資料（網路與畫面內容不同時會有差異）：

| 項目 | 原始模式 | TV 監控模式 |
|---|---:|---:|
| 畫面尺寸 | 1920×1080 | 960×540 |
| 單張大小 | 約 2.4–3.4 MB PNG | 約 6.4 KB JPEG |
| 第一張畫面 | 約 4.4 秒 | 約 1.25 秒 |

這個模式的目標是可靠的準即時監控，而不是 30/60 FPS 影片串流。TV
screenshot 模式目前不產生測試 MP4 錄影。

## 相關程式位置

- `webapp/frontend/src/composables/useDeviceScreen.js`
  - 裝置類型辨識
  - H.264／screenshot 選擇與 fallback
  - 首幀 timeout、自動重連、Canvas 顯示
- `webapp/livescreen/server.py`
  - ADB H.264 與 screenshot 擷取
  - ffmpeg 等比例縮圖與 JPEG 壓縮
  - 同裝置串流互斥與舊程序清理
  - 靜止畫面的 idle flush、`screenrecord` 自述錯誤的記錄
- `autoperf/screen_stream.py`
  - Annex-B NAL 切割及 H.264 access unit 組裝
  - `flush()`：把沒有下一個 start code 可以收尾的 NAL 送出

## 調整參數

| 參數 | 目前值 | 程式位置 |
|---|---:|---|
| H.264 首幀 timeout | 20 秒 | `FIRST_FRAME_TIMEOUT_MS` |
| 靜止畫面 idle flush | 1 秒 | `IDLE_FLUSH_SECONDS`（`livescreen/server.py`） |
| 重連退避 | 1、2、5、10 秒 | `RECONNECT_DELAYS_MS` |
| TV screenshot 間隔 | 1.2 秒 | `startFallback()` |
| TV 最大寬度 | 960 px | `startFallback()` |
| 手機 fallback 間隔 | 0.7 秒 | `startFallback()` |
| 手機 fallback 最大寬度 | 720 px | `startFallback()` |
| 伺服器允許的最大寬度 | 1920 px | `handler()` |

WebSocket screenshot 參數為 `mode=screenshot`、`interval=<秒>` 與
`max_width=<像素>`。伺服器會將間隔限制在 0.4–5 秒、寬度限制在
0–1920 px，避免不合理的請求造成過量負載。
