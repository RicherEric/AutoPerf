<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelRun,
  connectDevice,
  listDevices,
  listRuns,
  pairDevice,
  refreshDevices,
  setDeviceNickname,
  triggerRun,
} from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
const STATUS_TONE = {
  completed: 'success',
  running: 'warning',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'danger',
}

const devices = ref([])
const runs = ref([])
const error = ref('')
const info = ref('')

// Most-recent pending/running run per device, so "Start test" can't fire a
// second concurrent run for a phone that already has one in flight/queued.
const activeRunBySerial = computed(() => {
  const map = {}
  for (const run of runs.value) {
    if ((run.status === 'running' || run.status === 'pending') && !map[run.device_serial]) {
      map[run.device_serial] = run
    }
  }
  return map
})

const pairAddress = ref('')
const pairCode = ref('')
const pairing = ref(false)

const connectAddress = ref('')
const connecting = ref(false)

const startingSerial = ref('')
const cancellingSerial = ref('')

let pollHandle = null

async function loadDevices() {
  devices.value = await refreshDevices()
}

async function onPair() {
  error.value = ''
  info.value = ''
  pairing.value = true
  try {
    const result = await pairDevice(pairAddress.value.trim(), pairCode.value.trim())
    info.value = t('join.pairSuccess', { message: result.message })
    pairAddress.value = ''
    pairCode.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    pairing.value = false
  }
}

async function onConnect() {
  error.value = ''
  info.value = ''
  connecting.value = true
  try {
    const result = await connectDevice(connectAddress.value.trim())
    devices.value = result.devices
    info.value = t('join.connectSuccess', { message: result.message })
    connectAddress.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    connecting.value = false
  }
}

async function onSetNickname(serial, nickname) {
  error.value = ''
  try {
    await setDeviceNickname(serial, nickname)
    await loadDevices()
  } catch (err) {
    error.value = err.message
  }
}

async function onStartTest(serial) {
  error.value = ''
  startingSerial.value = serial
  try {
    await triggerRun(serial, 30)
    runs.value = await listRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    startingSerial.value = ''
  }
}

async function onCancelRun(runId, serial) {
  error.value = ''
  cancellingSerial.value = serial
  try {
    await cancelRun(runId)
    runs.value = await listRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingSerial.value = ''
  }
}

async function poll() {
  try {
    ;[devices.value, runs.value] = await Promise.all([listDevices(), listRuns()])
  } catch {
    // transient poll failure -- next tick retries
  }
}

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, 3000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('join.title')">
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>
    <p class="hint">{{ t('join.intro') }}</p>
    <ol class="steps">
      <li>{{ t('join.step1') }}</li>
      <li>{{ t('join.step2') }}</li>
      <li>{{ t('join.step3') }}</li>
      <li>{{ t('join.step4') }}</li>
      <li>{{ t('join.step5') }}</li>
      <li>{{ t('join.step6') }}</li>
    </ol>
  </Card>

  <Card :title="t('join.pairSectionTitle')">
    <div class="form-row">
      <input v-model="pairAddress" :placeholder="t('join.pairAddressPlaceholder')" />
      <input v-model="pairCode" :placeholder="t('join.pairCodePlaceholder')" maxlength="6" />
      <button @click="onPair" :disabled="pairing || !pairAddress.trim() || !pairCode.trim()">
        {{ pairing ? t('join.pairing') : t('join.pairButton') }}
      </button>
    </div>
  </Card>

  <Card :title="t('join.connectSectionTitle')">
    <div class="form-row">
      <input v-model="connectAddress" :placeholder="t('join.connectAddressPlaceholder')" />
      <button @click="onConnect" :disabled="connecting || !connectAddress.trim()">
        {{ connecting ? t('join.connecting') : t('join.connectButton') }}
      </button>
    </div>
  </Card>

  <Card :title="t('join.joinedSectionTitle')">
    <p v-if="!devices.length" class="hint">{{ t('join.noDevicesYet') }}</p>
    <table v-else>
      <thead>
        <tr>
          <th>{{ t('join.colNickname') }}</th>
          <th>{{ t('join.colModel') }}</th>
          <th>{{ t('join.colConnection') }}</th>
          <th>{{ t('join.colBattery') }}</th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.serial">
          <td>
            <input
              class="nickname-input"
              :value="d.nickname ?? ''"
              :placeholder="t('join.nicknamePlaceholder')"
              @change="onSetNickname(d.serial, $event.target.value)"
            />
          </td>
          <td>{{ d.model }}</td>
          <td>
            <StatusBadge
              :label="d.connection === 'wifi' ? 'WiFi' : 'USB'"
              :tone="d.connection === 'wifi' ? 'success' : 'neutral'"
            />
          </td>
          <td>{{ d.battery_level ?? '—' }}{{ d.battery_level != null ? '%' : '' }}</td>
          <td>
            <details class="device-details">
              <summary>{{ t('common.moreInfo') }}</summary>
              <dl>
                <dt>{{ t('common.manufacturer') }}</dt><dd>{{ d.manufacturer ?? '—' }} / {{ d.brand ?? '—' }}</dd>
                <dt>{{ t('common.sdkVersion') }}</dt><dd>{{ d.sdk_version ?? '—' }}</dd>
                <dt>{{ t('common.buildId') }}</dt><dd>{{ d.build_id ?? '—' }}</dd>
                <dt>{{ t('common.cpuAbi') }}</dt><dd>{{ d.cpu_abi ?? '—' }}</dd>
                <dt>{{ t('common.chromeVersion') }}</dt><dd>{{ d.chrome_version ?? '—' }}</dd>
                <dt>{{ t('common.wifiIp') }}</dt><dd>{{ d.wifi_ip ?? '—' }}</dd>
                <dt>{{ t('common.userAgent') }}</dt><dd class="ua">{{ d.user_agent ?? '—' }}</dd>
              </dl>
            </details>
          </td>
          <td>
            <button
              v-if="!activeRunBySerial[d.serial]"
              @click="onStartTest(d.serial)"
              :disabled="startingSerial === d.serial"
            >
              {{ startingSerial === d.serial ? t('join.starting') : t('join.startTestButton') }}
            </button>
            <template v-else>
              <StatusBadge
                :label="activeRunBySerial[d.serial].status"
                :tone="STATUS_TONE[activeRunBySerial[d.serial].status] ?? 'neutral'"
              />
              <button
                @click="onCancelRun(activeRunBySerial[d.serial].id, d.serial)"
                :disabled="cancellingSerial === d.serial"
              >
                {{ cancellingSerial === d.serial ? '…' : t('join.cancelButton') }}
              </button>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>
</template>

<style scoped>
.steps {
  padding-left: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.form-row {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
  align-items: center;
}
.form-row input {
  flex: 1;
  min-width: 200px;
}
.nickname-input {
  width: 100%;
  max-width: 160px;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.device-details summary {
  cursor: pointer;
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.device-details dl {
  margin: var(--space-2) 0 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--space-1) var(--space-3);
  max-width: 320px;
  font-size: 0.85em;
}
.device-details dt {
  color: var(--color-text-muted);
}
.device-details dd {
  margin: 0;
}
.device-details .ua {
  word-break: break-all;
}
.info {
  color: var(--color-success);
  font-size: 0.9em;
}
</style>
