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
- `autoperf/screen_stream.py`
  - Annex-B NAL 切割及 H.264 access unit 組裝

## 調整參數

| 參數 | 目前值 | 程式位置 |
|---|---:|---|
| H.264 首幀 timeout | 20 秒 | `FIRST_FRAME_TIMEOUT_MS` |
| 重連退避 | 1、2、5、10 秒 | `RECONNECT_DELAYS_MS` |
| TV screenshot 間隔 | 1.2 秒 | `startFallback()` |
| TV 最大寬度 | 960 px | `startFallback()` |
| 手機 fallback 間隔 | 0.7 秒 | `startFallback()` |
| 手機 fallback 最大寬度 | 720 px | `startFallback()` |
| 伺服器允許的最大寬度 | 1920 px | `handler()` |

WebSocket screenshot 參數為 `mode=screenshot`、`interval=<秒>` 與
`max_width=<像素>`。伺服器會將間隔限制在 0.4–5 秒、寬度限制在
0–1920 px，避免不合理的請求造成過量負載。
