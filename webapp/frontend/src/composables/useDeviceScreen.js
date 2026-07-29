import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { listDevices } from '../api.js'

const LIVESCREEN_HOST = 'ws://127.0.0.1:8100'
// The server now runs a pre-stream cleanup pass (_kill_stale_screenrecord)
// before spawning screenrecord, on top of the usual adb handshake + first
// SPS/PPS/IDR emission latency -- 3000ms was cutting it too close on a cold
// first connect, so most attempts fell back to screenshots even when H.264
// would have worked fine given another second or two.
const FIRST_FRAME_TIMEOUT_MS = 20000
const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000]

// Shared WebCodecs/canvas connect-decode-fallback logic behind a live
// `/stream/<serial>` WebSocket, extracted from the standalone device-screen
// page so it can also drive a small embedded panel (e.g. on Run Detail)
// without duplicating the decoder setup. Each call owns its own canvas/
// socket/decoder state -- multiple components can each call this
// independently to watch different devices at once (the livescreen server
// tracks one active stream per serial, not one globally).
export function useDeviceScreen() {
  const { t } = useI18n()

  const canvas = ref(null)
  const connectionState = ref('idle') // idle | connecting | streaming | fallback | retrying | error
  const errorMessage = ref('')
  // Array of pre-formatted strings (e.g. "CPU 12.3%"), or null to hide the
  // HUD -- the composable stays agnostic about metric semantics/formatting;
  // that's the caller's job via updateStats().
  const hudLines = ref(null)

  let socket = null
  let decoder = null
  let firstFrameTimer = null
  let gotFirstFrame = false
  let reconnectTimer = null
  let reconnectAttempt = 0
  let desiredConnection = null
  let manuallyStopped = false
  let connectGeneration = 0

  async function isTvDevice(serial) {
    try {
      const devices = await listDevices()
      const device = devices.find((item) => item.serial === serial)
      if (!device) return false
      const identity = [
        device.model,
        device.product,
        device.manufacturer,
        device.brand,
        device.device,
        device.nickname,
      ].filter(Boolean).join(' ').toLowerCase()
      return /(chromecast|google\s*tv|android\s*tv|smart\s*tv|sabrina|bravia|shield)/i.test(identity)
    } catch {
      // Device metadata is only an optimization. If it is temporarily
      // unavailable, retain the normal H.264 -> screenshot fallback.
      return false
    }
  }

  function updateStats(lines) {
    hudLines.value = lines && lines.length ? lines : null
  }

  function drawHud(ctx, canvasWidth) {
    if (!hudLines.value) return
    const padding = 8
    const lineHeight = 18
    ctx.save()
    ctx.font = '13px monospace'
    ctx.textBaseline = 'top'
    const textWidth = Math.max(...hudLines.value.map((line) => ctx.measureText(line).width))
    const boxWidth = textWidth + padding * 2
    const boxHeight = hudLines.value.length * lineHeight + padding * 2
    const boxX = canvasWidth - boxWidth - 8
    ctx.fillStyle = 'rgba(0, 0, 0, 0.55)'
    ctx.fillRect(boxX, 8, boxWidth, boxHeight)
    ctx.fillStyle = '#fff'
    hudLines.value.forEach((line, i) => {
      ctx.fillText(line, boxX + padding, 8 + padding + i * lineHeight)
    })
    ctx.restore()
  }

  function drawBitmapToCanvas(bitmap) {
    const el = canvas.value
    if (!el) return
    // A decoded VideoFrame has no plain .width/.height (only displayWidth/
    // displayHeight) -- only ImageBitmap (the screenshot fallback path) does.
    // Reading bitmap.width on a VideoFrame silently gives undefined, which
    // resizes the canvas to 0x0 and makes drawImage a no-op: exactly the
    // "streaming badge shows but the canvas stays blank" symptom.
    const width = bitmap.displayWidth ?? bitmap.width
    const height = bitmap.displayHeight ?? bitmap.height
    if (el.width !== width || el.height !== height) {
      el.width = width
      el.height = height
    }
    const ctx = el.getContext('2d')
    ctx.drawImage(bitmap, 0, 0, width, height)
    drawHud(ctx, width)
  }

  function stopEverything() {
    clearTimeout(firstFrameTimer)
    clearTimeout(reconnectTimer)
    if (decoder && decoder.state !== 'closed') {
      try {
        decoder.close()
      } catch {
        // already closing/closed -- nothing to do
      }
    }
    decoder = null
    if (socket) {
      socket.onclose = null
      socket.onerror = null
      socket.close()
    }
    socket = null
  }

  function scheduleReconnect() {
    if (manuallyStopped || !desiredConnection || reconnectTimer) return
    stopEverything()
    connectionState.value = 'retrying'
    const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)]
    reconnectAttempt += 1
    errorMessage.value = t('screen.reconnecting', {
      seconds: Math.round(delay / 1000),
      attempt: reconnectAttempt,
    })
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      if (!manuallyStopped && desiredConnection) {
        if (desiredConnection.preferScreenshot) {
          startFallback(desiredConnection.serial, desiredConnection.runId)
        } else {
          startH264(desiredConnection.serial, desiredConnection.runId)
        }
      }
    }, delay)
  }

  function parseCodecFromSps(bytes) {
    // bytes: the framed key access unit, starting right after our 1-byte
    // key/delta prefix. The first NAL after a 4-byte Annex-B start code is
    // always SPS (autoperf.screen_stream.AccessUnitAssembler bundles SPS
    // before PPS before the IDR, matching the device's own emission order).
    // profile_idc/constraint_flags/level_idc are the 3 bytes right after the
    // NAL header byte -- see ITU-T H.264 7.3.2.1.1.
    const nalStart = 4 // skip the 4-byte start code (00 00 00 01)
    const profileIdc = bytes[nalStart + 1]
    const constraintFlags = bytes[nalStart + 2]
    const levelIdc = bytes[nalStart + 3]
    const hex = (n) => n.toString(16).padStart(2, '0')
    return `avc1.${hex(profileIdc)}${hex(constraintFlags)}${hex(levelIdc)}`
  }

  function startFallback(serial, runId) {
    stopEverything()
    connectionState.value = 'fallback'
    // TV screenshots are much larger than phone screenshots (often 3+ MB).
    // A slower cadence keeps ADB and the browser responsive without delaying
    // the first frame; the server captures immediately before its first sleep.
    const isTv = desiredConnection?.preferScreenshot
    const interval = isTv ? 1.2 : 0.7
    const maxWidth = isTv ? 960 : 720
    socket = new WebSocket(
      `${LIVESCREEN_HOST}/stream/${serial}?mode=screenshot&interval=${interval}&max_width=${maxWidth}`,
    )
    // No run_id here: the server only records the h264 path (see
    // livescreen/server.py's _start_recording) -- there's no recording to
    // tie a screenshot-fallback session to anyway.
    socket.binaryType = 'arraybuffer'
    socket.onmessage = async (event) => {
      try {
        // The service normally sends a resized JPEG; if ffmpeg isn't
        // available it transparently falls back to the original PNG.
        // Leaving the MIME type unset lets the browser sniff either format.
        const bitmap = await createImageBitmap(new Blob([event.data]))
        drawBitmapToCanvas(bitmap)
        reconnectAttempt = 0
        errorMessage.value = ''
      } catch (err) {
        errorMessage.value = t('screen.screenshotDecodeFailed', { message: err.message })
      }
    }
    socket.onerror = () => {
      errorMessage.value = t('screen.screenshotConnectionFailed')
      scheduleReconnect()
    }
    socket.onclose = () => scheduleReconnect()
  }

  function startH264(serial, runId) {
    connectionState.value = 'connecting'
    gotFirstFrame = false
    let configured = false // tracked locally rather than re-reading decoder.state --
    // configure() queues the state transition on VideoDecoder's internal control
    // message queue, which is not guaranteed to be reflected in `decoder.state`
    // synchronously by the time the very next line runs. Re-checking
    // `decoder.state === 'configured'` right after calling configure() could
    // see the stale 'unconfigured' value and skip decode()'ing the key frame --
    // and without a decoded key frame, delta frames alone can never produce a
    // picture. Calling decode() unconditionally right after configure() is safe:
    // the queue guarantees configure() runs first.
    let messageCount = 0
    decoder = new VideoDecoder({
      output: (frame) => {
        gotFirstFrame = true
        reconnectAttempt = 0
        connectionState.value = 'streaming'
        errorMessage.value = ''
        clearTimeout(firstFrameTimer)
        drawBitmapToCanvas(frame)
        frame.close()
      },
      error: (err) => {
        console.error('VideoDecoder error', err)
        errorMessage.value = t('screen.decoderError', { message: err.message })
        startFallback(serial, runId)
      },
    })

    // A run_id here (when watching from Run Detail / Mission Control) tells
    // the server to also remux this stream to an MP4 via ffmpeg for later
    // replay -- see livescreen/server.py's _start_recording.
    const streamUrl = runId
      ? `${LIVESCREEN_HOST}/stream/${serial}?run_id=${encodeURIComponent(runId)}`
      : `${LIVESCREEN_HOST}/stream/${serial}`
    socket = new WebSocket(streamUrl)
    socket.binaryType = 'arraybuffer'
    socket.onmessage = (event) => {
      messageCount += 1
      const data = new Uint8Array(event.data)
      const isKey = data[0] === 1
      const payload = data.subarray(1)
      try {
        if (isKey && !configured) {
          const codec = parseCodecFromSps(payload)
          console.log('Configuring VideoDecoder', codec)
          // No `description` and no `avc` field, matching the real, working
          // @yume-chan/scrcpy-decoder-webcodecs implementation (used by
          // ws-scrcpy/tango): omitting `description` for an 'avc1.*' codec is
          // what makes Chrome treat the bitstream as Annex-B, since AVC/AVCC
          // format requires a description (the avcC box) that we don't have
          // and don't need. The `avc: {format: 'annexb'}` field used in an
          // earlier version of this file is not part of what that proven
          // implementation sends and may be silently ignored or mishandled.
          decoder.configure({ codec, hardwareAcceleration: 'no-preference', optimizeForLatency: true })
          configured = true
        }
        if (configured) {
          decoder.decode(new EncodedVideoChunk({
            type: isKey ? 'key' : 'delta',
            timestamp: performance.now() * 1000,
            data: payload,
          }))
        }
      } catch (err) {
        console.error('WebCodecs decode failed', err)
        errorMessage.value = t('screen.decodeFailed', { message: err.message })
        startFallback(serial)
      }
    }
    socket.onerror = () => {
      errorMessage.value = t('screen.connectionFailed')
      startFallback(serial, runId)
    }
    socket.onclose = () => {
      if (!gotFirstFrame) startFallback(serial, runId)
      else scheduleReconnect()
    }

    firstFrameTimer = setTimeout(() => {
      if (!gotFirstFrame) {
        console.warn(`No frame after ${FIRST_FRAME_TIMEOUT_MS}ms: received ${messageCount} WS message(s), configured=${configured}`)
        errorMessage.value = t('screen.noFrameArrived', { count: messageCount })
        startFallback(serial, runId)
      }
    }, FIRST_FRAME_TIMEOUT_MS)
  }

  async function connect(serial, { runId } = {}) {
    const generation = ++connectGeneration
    errorMessage.value = ''
    manuallyStopped = false
    desiredConnection = { serial, runId, preferScreenshot: false }
    reconnectAttempt = 0
    stopEverything()

    // Chromecast / Android TV frequently takes a long time to start
    // screenrecord and may not emit an IDR before the first-frame timeout.
    // Discover the form factor first and go directly to the reliable
    // screenshot transport instead of making the user wait for that timeout.
    const preferScreenshot = await isTvDevice(serial)
    if (generation !== connectGeneration || manuallyStopped || desiredConnection?.serial !== serial) return
    desiredConnection.preferScreenshot = preferScreenshot
    if (preferScreenshot) {
      startFallback(serial, runId)
      return
    }
    if (typeof VideoDecoder === 'undefined') {
      errorMessage.value = t('screen.noWebCodecs')
      startFallback(serial, runId)
    } else {
      startH264(serial, runId)
    }
  }

  function disconnect() {
    connectGeneration += 1
    manuallyStopped = true
    desiredConnection = null
    stopEverything()
    connectionState.value = 'idle'
    hudLines.value = null
  }

  return { canvas, connectionState, errorMessage, connect, disconnect, updateStats }
}
