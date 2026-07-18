<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelRun,
  connectDevice,
  deleteRun,
  listDevices,
  listRuns,
  listYoutubeScenarios,
  refreshDevices,
  setDeviceNickname,
  triggerRun,
  triggerSuite,
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

// Shallow to deep, mirroring smoke-test -> full regression-suite conventions:
// smoke only checks "does the app launch and reach basic content" (fast, run
// often); functional covers everyday interactions; regression covers
// deeper/edge-case flows meant for a slower cadence (e.g. nightly), precisely
// because they take longer and touch less-common paths.
const TIER_ORDER = ['smoke', 'functional', 'regression']

const devices = ref([])
const runs = ref([])
const youtubeScenarios = ref([]) // [{ name, description, tier }]
const selectedSerial = ref('')
const selectedScenario = ref('')
const duration = ref(60)
const error = ref('')
const refreshing = ref(false)
const starting = ref(false)
const startingSuite = ref('')
const cancellingId = ref('')
const connectAddress = ref('')
const connecting = ref(false)
const selectedIds = ref(new Set())
const bulkDeleting = ref(false)

let pollHandle = null

function deviceLabel(d) {
  const name = d.nickname || d.model
  const badge = d.connection === 'wifi' ? 'WiFi' : 'USB'
  return `${name} (${d.serial}) [${badge}]`
}

// The raw ISO timestamp (with microseconds + timezone offset, e.g.
// "2026-07-16T17:47:04.086985+00:00") is ~32 characters wide -- in a table
// column that alone was pushing the Actions column (Cancel/Delete) off the
// visible edge of the card. A short local-time format fits comfortably.
function formatTimestamp(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { hour12: false })
}

function isDeletable(run) {
  return run.status !== 'running' && run.status !== 'pending'
}

const deletableRuns = computed(() => runs.value.filter(isDeletable))
const allDeletableSelected = computed(() =>
  deletableRuns.value.length > 0 && deletableRuns.value.every((r) => selectedIds.value.has(r.id))
)

function toggleSelected(runId) {
  const next = new Set(selectedIds.value)
  if (next.has(runId)) {
    next.delete(runId)
  } else {
    next.add(runId)
  }
  selectedIds.value = next
}

function toggleSelectAll() {
  if (allDeletableSelected.value) {
    selectedIds.value = new Set()
  } else {
    selectedIds.value = new Set(deletableRuns.value.map((r) => r.id))
  }
}

const scenariosByTier = computed(() => {
  const grouped = {}
  for (const scenario of youtubeScenarios.value) {
    if (!grouped[scenario.tier]) grouped[scenario.tier] = []
    grouped[scenario.tier].push(scenario)
  }
  return grouped
})

const selectedScenarioInfo = computed(() =>
  youtubeScenarios.value.find((s) => s.name === selectedScenario.value) ?? null
)

async function loadDevices() {
  devices.value = await listDevices()
  if (!selectedSerial.value && devices.value.length) {
    selectedSerial.value = devices.value[0].serial
  }
}

async function loadRuns() {
  runs.value = await listRuns()
}

async function onRefreshDevices() {
  error.value = ''
  refreshing.value = true
  try {
    devices.value = await refreshDevices()
    if (!selectedSerial.value && devices.value.length) {
      selectedSerial.value = devices.value[0].serial
    }
  } catch (err) {
    error.value = err.message
  } finally {
    refreshing.value = false
  }
}

async function onStartRun() {
  error.value = ''
  starting.value = true
  try {
    await triggerRun(selectedSerial.value, Number(duration.value), selectedScenario.value)
    await loadRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    starting.value = false
  }
}

async function onRunSuite(tier) {
  error.value = ''
  startingSuite.value = tier
  try {
    await triggerSuite(selectedSerial.value, tier, Number(duration.value))
    await loadRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    startingSuite.value = ''
  }
}

async function onBulkDelete() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  if (!confirm(t('runs.confirmBulkDelete', { count: ids.length }))) return
  error.value = ''
  bulkDeleting.value = true
  try {
    const results = await Promise.allSettled(ids.map((id) => deleteRun(id)))
    const failures = results.filter((r) => r.status === 'rejected')
    if (failures.length) {
      error.value = t('runs.bulkDeleteFailure', {
        failed: failures.length, total: ids.length, message: failures[0].reason.message,
      })
    }
    selectedIds.value = new Set()
    await loadRuns()
  } finally {
    bulkDeleting.value = false
  }
}

async function onCancelRun(runId) {
  error.value = ''
  cancellingId.value = runId
  try {
    await cancelRun(runId)
    await loadRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingId.value = ''
  }
}

async function onConnectDevice() {
  error.value = ''
  connecting.value = true
  try {
    const result = await connectDevice(connectAddress.value.trim())
    devices.value = result.devices
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

onMounted(async () => {
  await Promise.all([
    loadDevices(),
    loadRuns(),
    listYoutubeScenarios().then((entries) => (youtubeScenarios.value = entries)),
  ])
  pollHandle = setInterval(loadRuns, 3000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('runs.devicesTitle')">
    <p v-if="error" class="error">{{ error }}</p>
    <div class="connect-row">
      <input v-model="connectAddress" placeholder="192.168.1.50:5555" @keyup.enter="onConnectDevice" />
      <button @click="onConnectDevice" :disabled="connecting || !connectAddress.trim()">
        {{ connecting ? t('runs.connecting') : t('runs.connectButton') }}
      </button>
      <button @click="onRefreshDevices" :disabled="refreshing">
        {{ refreshing ? t('runs.refreshing') : t('runs.refreshButton') }}
      </button>
    </div>
    <table>
      <thead>
        <tr>
          <th>{{ t('runs.colNickname') }}</th>
          <th>{{ t('runs.colModel') }}</th>
          <th>{{ t('runs.colSerial') }}</th>
          <th>{{ t('runs.colConnection') }}</th>
          <th>{{ t('runs.colAndroid') }}</th>
          <th>{{ t('runs.colBattery') }}</th>
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
              :placeholder="t('common.unnamed')"
              @change="onSetNickname(d.serial, $event.target.value)"
            />
          </td>
          <td>{{ d.model }}</td>
          <td>{{ d.serial }}</td>
          <td>
            <StatusBadge
              :label="d.connection === 'wifi' ? 'WiFi' : 'USB'"
              :tone="d.connection === 'wifi' ? 'success' : 'neutral'"
            />
          </td>
          <td>{{ d.android_version ?? '—' }}</td>
          <td>{{ d.battery_level ?? '—' }}{{ d.battery_level != null ? '%' : '' }}</td>
          <td>
            <button v-if="d.serial !== selectedSerial" @click="selectedSerial = d.serial">{{ t('common.select') }}</button>
            <StatusBadge v-else :label="t('common.selected')" tone="success" />
          </td>
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
        </tr>
      </tbody>
    </table>
  </Card>

  <Card :title="t('runs.startRunTitle')">
    <div>
      <label>
        {{ t('runs.deviceLabel') }}
        <select v-model="selectedSerial">
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d) }}
          </option>
        </select>
      </label>
    </div>
    <div>
      <label>
        {{ t('runs.durationLabel') }}
        <input type="number" v-model="duration" min="1" />
      </label>
      <label>
        {{ t('runs.scenarioLabel') }}
        <select v-model="selectedScenario">
          <option value="">{{ t('runs.noneOption') }}</option>
          <optgroup v-for="tier in TIER_ORDER" :key="tier" :label="t(`device.tierLabels.${tier}`)">
            <option
              v-for="scenario in scenariosByTier[tier] ?? []"
              :key="scenario.name"
              :value="scenario.name"
              :title="scenario.description"
            >
              {{ scenario.name }}
            </option>
          </optgroup>
        </select>
      </label>
      <button @click="onStartRun" :disabled="starting || !selectedSerial">
        {{ starting ? t('runs.starting') : t('runs.startButton') }}
      </button>
    </div>
    <p v-if="selectedScenarioInfo" class="hint">{{ selectedScenarioInfo.description }}</p>
  </Card>

  <Card :title="t('runs.suiteTitle')">
    <p class="hint">{{ t('runs.suiteHint') }}</p>
    <div class="suite-buttons">
      <button
        v-for="tier in TIER_ORDER"
        :key="tier"
        @click="onRunSuite(tier)"
        :disabled="!!startingSuite || !selectedSerial"
      >
        {{ startingSuite === tier ? t('runs.starting') : t('runs.suiteButton', { tier, count: (scenariosByTier[tier] ?? []).length }) }}
      </button>
    </div>
  </Card>

  <Card :title="t('runs.runsTitle')">
    <div class="bulk-bar">
      <button @click="onBulkDelete" :disabled="!selectedIds.size || bulkDeleting">
        {{ bulkDeleting ? t('common.deleting') : t('runs.bulkDeleteButton', { count: selectedIds.size }) }}
      </button>
    </div>
    <table class="runs-table">
      <thead>
        <tr>
          <th><input type="checkbox" :checked="allDeletableSelected" @change="toggleSelectAll" /></th>
          <th>{{ t('runs.colId') }}</th>
          <th>{{ t('runs.colDevice') }}</th>
          <th>{{ t('runs.colStatus') }}</th>
          <th>{{ t('runs.colStarted') }}</th>
          <th>{{ t('runs.colFinished') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.id">
          <td>
            <input
              type="checkbox"
              :checked="selectedIds.has(run.id)"
              :disabled="!isDeletable(run)"
              @change="toggleSelected(run.id)"
            />
          </td>
          <td><router-link :to="`/runs/${run.id}`">{{ run.id.slice(0, 8) }}</router-link></td>
          <td>{{ run.device_serial }}</td>
          <td><StatusBadge :label="run.status" :tone="STATUS_TONE[run.status] ?? 'neutral'" /></td>
          <td>{{ formatTimestamp(run.started_at) }}</td>
          <td>{{ formatTimestamp(run.finished_at) }}</td>
          <td>
            <button
              v-if="run.status === 'running' || run.status === 'pending'"
              @click="onCancelRun(run.id)"
              :disabled="cancellingId === run.id"
            >
              {{ cancellingId === run.id ? '…' : t('common.cancel') }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>
</template>

<style scoped>
.runs-table td {
  white-space: nowrap;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.suite-buttons {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.bulk-bar {
  margin-bottom: var(--space-3);
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
.connect-row {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-bottom: var(--space-3);
}
.connect-row input {
  flex: 1;
  max-width: 260px;
}
.nickname-input {
  width: 100%;
  max-width: 160px;
}
</style>
