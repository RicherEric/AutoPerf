import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

const LIVESCREEN_HOST = 'ws://127.0.0.1:8100'
// The server now runs a pre-stream cleanup pass (_kill_stale_screenrecord)
// before spawning screenrecord, on top of the usual adb handshake + first
// SPS/PPS/IDR emission latency -- 3000ms was cutting it too close on a cold
// first connect, so most attempts fell back to screenshots even when H.264
// would have worked fine given another second or two.
const FIRST_FRAME_TIMEOUT_MS = 6000

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
  const connectionState = ref('idle') // idle | connecting | streaming | fallback | error
  const errorMessage = ref('')

  let socket = null
  let decoder = null
  let firstFrameTimer = null
  let gotFirstFrame = false

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
  }

  function stopEverything() {
    clearTimeout(firstFrameTimer)
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

  function startFallback(serial) {
    stopEverything()
    connectionState.value = 'fallback'
    socket = new WebSocket(`${LIVESCREEN_HOST}/stream/${serial}?mode=screenshot`)
    socket.binaryType = 'arraybuffer'
    socket.onmessage = async (event) => {
      try {
        const bitmap = await createImageBitmap(new Blob([event.data], { type: 'image/png' }))
        drawBitmapToCanvas(bitmap)
      } catch (err) {
        errorMessage.value = t('screen.screenshotDecodeFailed', { message: err.message })
      }
    }
    socket.onerror = () => {
      connectionState.value = 'error'
      errorMessage.value = t('screen.screenshotConnectionFailed')
    }
  }

  function startH264(serial) {
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
        connectionState.value = 'streaming'
        clearTimeout(firstFrameTimer)
        drawBitmapToCanvas(frame)
        frame.close()
      },
      error: (err) => {
        console.error('VideoDecoder error', err)
        errorMessage.value = t('screen.decoderError', { message: err.message })
        startFallback(serial)
      },
    })

    socket = new WebSocket(`${LIVESCREEN_HOST}/stream/${serial}`)
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
      startFallback(serial)
    }

    firstFrameTimer = setTimeout(() => {
      if (!gotFirstFrame) {
        console.warn(`No frame after ${FIRST_FRAME_TIMEOUT_MS}ms: received ${messageCount} WS message(s), configured=${configured}`)
        errorMessage.value = t('screen.noFrameArrived', { count: messageCount })
        startFallback(serial)
      }
    }, FIRST_FRAME_TIMEOUT_MS)
  }

  function connect(serial) {
    errorMessage.value = ''
    stopEverything()
    if (typeof VideoDecoder === 'undefined') {
      errorMessage.value = t('screen.noWebCodecs')
      startFallback(serial)
    } else {
      startH264(serial)
    }
  }

  function disconnect() {
    stopEverything()
    connectionState.value = 'idle'
  }

  return { canvas, connectionState, errorMessage, connect, disconnect }
}
