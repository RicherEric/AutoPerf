<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { controlDevice, listDevices } from '../api.js'
import { useDeviceScreen } from '../composables/useDeviceScreen.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
const { canvas, connectionState, errorMessage, connect, disconnect } = useDeviceScreen()

const devices = ref([])
const selectedSerial = ref('')
const controlError = ref('')

let switching = false // suppresses the auto-reconnect watcher while Prev/Next itself drives the change

function deviceLabel(d) {
  const name = d.nickname || d.model
  const badge = d.connection === 'wifi' ? 'WiFi' : 'USB'
  return `${name} (${d.serial}) [${badge}]`
}

const selectedIndex = computed(() => devices.value.findIndex((d) => d.serial === selectedSerial.value))

async function loadDevices() {
  devices.value = await listDevices()
  if (!selectedSerial.value && devices.value.length) {
    selectedSerial.value = devices.value[0].serial
  }
}

function onConnect() {
  if (selectedSerial.value) connect(selectedSerial.value)
}

function switchTo(serial) {
  switching = true
  selectedSerial.value = serial
  onConnect()
  switching = false
}

function onPrevDevice() {
  if (devices.value.length < 2) return
  const idx = (selectedIndex.value - 1 + devices.value.length) % devices.value.length
  switchTo(devices.value[idx].serial)
}

function onNextDevice() {
  if (devices.value.length < 2) return
  const idx = (selectedIndex.value + 1) % devices.value.length
  switchTo(devices.value[idx].serial)
}

async function sendControl(action, payload = {}) {
  if (!selectedSerial.value) return
  controlError.value = ''
  try {
    await controlDevice(selectedSerial.value, action, payload)
  } catch (err) {
    controlError.value = err.message
  }
}

function onCanvasClick(event) {
  const el = canvas.value
  if (!el || !el.width || !el.height) return
  const rect = el.getBoundingClientRect()
  const x = Math.round((event.clientX - rect.left) * el.width / rect.width)
  const y = Math.round((event.clientY - rect.top) * el.height / rect.height)
  sendControl('tap', { x, y })
}

watch(selectedSerial, (next, prev) => {
  // Quick-switch UX: once a stream is already up, picking a different device
  // from the dropdown jumps straight to it instead of requiring a manual
  // "Connect" click each time -- this is the "one device at a time, fast
  // switching" mode the classroom demo needs. `switching` guards against a
  // redundant double-connect when switchTo() (Prev/Next) already reconnected.
  if (switching || !prev || next === prev) return
  if (connectionState.value !== 'idle') onConnect()
})

onMounted(loadDevices)
onUnmounted(disconnect)
</script>

<template>
  <Card :title="t('screen.title')">
    <p v-if="errorMessage" class="error">{{ errorMessage }}</p>
    <div class="controls">
      <label>
        {{ t('screen.deviceLabel') }}
        <select v-model="selectedSerial">
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d) }}
          </option>
        </select>
      </label>
      <button @click="onConnect" :disabled="!selectedSerial">{{ t('screen.connectButton') }}</button>
      <button @click="onPrevDevice" :disabled="devices.length < 2" :title="t('screen.prevTitle')">{{ t('screen.prevButton') }}</button>
      <button @click="onNextDevice" :disabled="devices.length < 2" :title="t('screen.nextTitle')">{{ t('screen.nextButton') }}</button>
      <StatusBadge
        v-if="connectionState !== 'idle'"
        :label="t(`screen.state.${connectionState}`)"
        :tone="connectionState === 'streaming' ? 'success' : connectionState === 'error' ? 'danger' : 'warning'"
      />
    </div>
    <p class="hint">{{ t('screen.hint') }}</p>
    <div class="screen-layout">
      <canvas ref="canvas" class="screen-canvas" @click="onCanvasClick"></canvas>
      <div class="remote-control">
        <h3>{{ t('screen.controllerTitle') }}</h3>
        <p v-if="controlError" class="error">{{ controlError }}</p>
        <div class="dpad">
          <button class="up" @click="sendControl('up')">▲</button>
          <button class="left" @click="sendControl('left')">◀</button>
          <button class="enter" @click="sendControl('enter')">OK</button>
          <button class="right" @click="sendControl('right')">▶</button>
          <button class="down" @click="sendControl('down')">▼</button>
        </div>
        <div class="system-buttons">
          <button @click="sendControl('back')">{{ t('screen.backButton') }}</button>
          <button @click="sendControl('home')">{{ t('screen.homeButton') }}</button>
        </div>
        <p class="hint">{{ t('screen.controllerHint') }}</p>
      </div>
    </div>
  </Card>
</template>

<style scoped>
.controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.screen-canvas {
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  margin-top: var(--space-3);
  background: var(--color-surface-alt);
  cursor: crosshair;
  min-width: 0;
}
.screen-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px;
  gap: var(--space-4);
  align-items: start;
}
.remote-control {
  margin-top: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.remote-control h3 { margin-top: 0; }
.dpad {
  display: grid;
  grid-template: repeat(3, 48px) / repeat(3, 48px);
  justify-content: center;
  gap: var(--space-1);
}
.dpad .up { grid-area: 1 / 2; }
.dpad .left { grid-area: 2 / 1; }
.dpad .enter { grid-area: 2 / 2; }
.dpad .right { grid-area: 2 / 3; }
.dpad .down { grid-area: 3 / 2; }
.system-buttons {
  display: flex;
  justify-content: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
@media (max-width: 760px) {
  .screen-layout { grid-template-columns: 1fr; }
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
</style>
