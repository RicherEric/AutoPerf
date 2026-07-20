<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { listDevices } from '../api.js'
import { useDeviceScreen } from '../composables/useDeviceScreen.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
const { canvas, connectionState, errorMessage, connect, disconnect } = useDeviceScreen()

const devices = ref([])
const selectedSerial = ref('')

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
    <canvas ref="canvas" class="screen-canvas"></canvas>
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
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
</style>
