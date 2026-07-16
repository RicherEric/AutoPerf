<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { listDevices, listRuns, listYoutubeScenarios, refreshDevices, triggerRun, triggerSuite } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

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
const TIER_LABELS = {
  smoke: 'Smoke（快速存活檢查）',
  functional: 'Functional（常見互動）',
  regression: 'Regression（深度／每日回歸）',
}
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

let pollHandle = null

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
  <Card title="Start a run">
    <p v-if="error" class="error">{{ error }}</p>
    <div>
      <label>
        Device:
        <select v-model="selectedSerial">
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ d.model }} ({{ d.serial }})
          </option>
        </select>
      </label>
      <button @click="onRefreshDevices" :disabled="refreshing">
        {{ refreshing ? 'Refreshing…' : 'Refresh devices' }}
      </button>
    </div>
    <div>
      <label>
        Duration (s):
        <input type="number" v-model="duration" min="1" />
      </label>
      <label>
        YouTube scenario:
        <select v-model="selectedScenario">
          <option value="">(none — plain run)</option>
          <optgroup v-for="tier in TIER_ORDER" :key="tier" :label="TIER_LABELS[tier] ?? tier">
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
        {{ starting ? 'Starting…' : 'Start Run' }}
      </button>
    </div>
    <p v-if="selectedScenarioInfo" class="hint">{{ selectedScenarioInfo.description }}</p>
  </Card>

  <Card title="Run a whole tier as a suite">
    <p class="hint">
      每個 tier 會依序把該分類下所有腳本各跑一次(各自獨立的 run),適合從快速 smoke
      檢查一路做到完整的每日 regression。每個腳本都使用上方設定的 Duration。
    </p>
    <div class="suite-buttons">
      <button
        v-for="tier in TIER_ORDER"
        :key="tier"
        @click="onRunSuite(tier)"
        :disabled="!!startingSuite || !selectedSerial"
      >
        {{ startingSuite === tier ? 'Starting…' : `Run ${tier} suite (${(scenariosByTier[tier] ?? []).length})` }}
      </button>
    </div>
  </Card>

  <Card title="Runs">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Device</th>
          <th>Status</th>
          <th>Started</th>
          <th>Finished</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.id">
          <td><router-link :to="`/runs/${run.id}`">{{ run.id.slice(0, 8) }}</router-link></td>
          <td>{{ run.device_serial }}</td>
          <td><StatusBadge :label="run.status" :tone="STATUS_TONE[run.status] ?? 'neutral'" /></td>
          <td>{{ run.started_at ?? '—' }}</td>
          <td>{{ run.finished_at ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
  </Card>
</template>

<style scoped>
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.suite-buttons {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}
</style>
