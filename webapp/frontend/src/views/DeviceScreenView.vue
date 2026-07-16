<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { listDevices } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const LIVESCREEN_HOST = 'ws://127.0.0.1:8100'
const FIRST_FRAME_TIMEOUT_MS = 3000

const devices = ref([])
const selectedSerial = ref('')
const connectionState = ref('idle') // idle | connecting | streaming | fallback | error
const errorMessage = ref('')
const canvas = ref(null)

let socket = null
let decoder = null
let firstFrameTimer = null
let gotFirstFrame = false

async function loadDevices() {
  devices.value = await listDevices()
  if (!selectedSerial.value && devices.value.length) {
    selectedSerial.value = devices.value[0].serial
  }
}

function drawBitmapToCanvas(bitmap) {
  const el = canvas.value
  if (!el) return
  if (el.width !== bitmap.width || el.height !== bitmap.height) {
    el.width = bitmap.width
    el.height = bitmap.height
  }
  const ctx = el.getContext('2d')
  ctx.drawImage(bitmap, 0, 0)
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

function startFallback() {
  stopEverything()
  connectionState.value = 'fallback'
  socket = new WebSocket(`${LIVESCREEN_HOST}/stream/${selectedSerial.value}?mode=screenshot`)
  socket.binaryType = 'arraybuffer'
  socket.onmessage = async (event) => {
    try {
      const bitmap = await createImageBitmap(new Blob([event.data], { type: 'image/png' }))
      drawBitmapToCanvas(bitmap)
    } catch (err) {
      errorMessage.value = `Failed to decode screenshot frame: ${err.message}`
    }
  }
  socket.onerror = () => {
    connectionState.value = 'error'
    errorMessage.value = 'Screenshot stream connection failed.'
  }
}

function startH264() {
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
      errorMessage.value = `WebCodecs decoder error, falling back: ${err.message}`
      startFallback()
    },
  })

  socket = new WebSocket(`${LIVESCREEN_HOST}/stream/${selectedSerial.value}`)
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
      errorMessage.value = `WebCodecs decode failed, falling back: ${err.message}`
      startFallback()
    }
  }
  socket.onerror = () => {
    errorMessage.value = 'H.264 stream connection failed, falling back to screenshots.'
    startFallback()
  }

  firstFrameTimer = setTimeout(() => {
    if (!gotFirstFrame) {
      console.warn(`No frame after ${FIRST_FRAME_TIMEOUT_MS}ms: received ${messageCount} WS message(s), configured=${configured}`)
      errorMessage.value = `No video frame arrived in time (received ${messageCount} message(s) from the server), falling back to screenshots.`
      startFallback()
    }
  }, FIRST_FRAME_TIMEOUT_MS)
}

function onConnect() {
  errorMessage.value = ''
  stopEverything()
  if (typeof VideoDecoder === 'undefined') {
    errorMessage.value = 'This browser has no WebCodecs support; using periodic screenshots.'
    startFallback()
  } else {
    startH264()
  }
}

onMounted(loadDevices)
onUnmounted(stopEverything)
</script>

<template>
  <Card title="Device screen">
    <p v-if="errorMessage" class="error">{{ errorMessage }}</p>
    <div>
      <label>
        Device:
        <select v-model="selectedSerial">
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ d.model }} ({{ d.serial }})
          </option>
        </select>
      </label>
      <button @click="onConnect" :disabled="!selectedSerial">Connect</button>
      <StatusBadge
        v-if="connectionState !== 'idle'"
        :label="connectionState"
        :tone="connectionState === 'streaming' ? 'success' : connectionState === 'error' ? 'danger' : 'warning'"
      />
    </div>
    <p class="hint">
      View-only for now — tapping the preview does not control the device.
      Requires <code>python webapp/livescreen/server.py --port 8100</code> running separately.
    </p>
    <canvas ref="canvas" class="screen-canvas"></canvas>
  </Card>
</template>

<style scoped>
.screen-canvas {
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  margin-top: var(--space-3);
  background: var(--color-surface-alt);
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
</style>
