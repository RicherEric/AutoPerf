<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { deleteRun, getComparison, getRun, listSamples, setBaseline } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DeltaBar from '../components/DeltaBar.vue'
import MetricChart from '../components/MetricChart.vue'

const props = defineProps({ id: String })
const router = useRouter()

const STATUS_TONE = {
  completed: 'success',
  running: 'warning',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'danger',
}
const PER_METRIC_CAP = 500
const RAW_TABLE_CAP = 200

const run = ref(null)
const comparison = ref(null)
const error = ref('')
const sinceId = ref(0)
const settingBaseline = ref(false)
const deleting = ref(false)

// Keyed by metric name so a fast collector (cpu/memory, every 5s) can't
// crowd out a slow one (battery, every 60s) in a single flat-capped array.
const samplesByMetric = reactive({})
const recentRaw = ref([])

let pollHandle = null
const TERMINAL_STATUSES = ['completed', 'failed', 'interrupted']

const metricNames = computed(() => Object.keys(samplesByMetric).sort())

const xDomain = computed(() => {
  const allTimes = metricNames.value.flatMap((name) =>
    samplesByMetric[name].map((s) => new Date(s.timestamp).getTime())
  )
  if (!allTimes.length) return [0, 1]
  return [Math.min(...allTimes), Math.max(...allTimes)]
})

const comparisonByMetric = computed(() => {
  const map = {}
  for (const metric of comparison.value?.metrics ?? []) {
    map[metric.name] = metric
  }
  return map
})

async function loadComparison() {
  comparison.value = await getComparison(props.id)
}

function bufferSamples(newSamples) {
  for (const sample of newSamples) {
    if (!samplesByMetric[sample.name]) {
      samplesByMetric[sample.name] = []
    }
    samplesByMetric[sample.name].push(sample)
    if (samplesByMetric[sample.name].length > PER_METRIC_CAP) {
      samplesByMetric[sample.name].shift()
    }
  }
  recentRaw.value = [...recentRaw.value, ...newSamples].slice(-RAW_TABLE_CAP)
}

async function poll() {
  try {
    run.value = await getRun(props.id)
    const result = await listSamples(props.id, sinceId.value)
    bufferSamples(result.samples)
    sinceId.value = result.next_since_id
    await loadComparison()
    if (run.value && TERMINAL_STATUSES.includes(run.value.status)) {
      clearInterval(pollHandle)
    }
  } catch (err) {
    error.value = err.message
    clearInterval(pollHandle)
  }
}

async function onSetBaseline() {
  error.value = ''
  settingBaseline.value = true
  try {
    await setBaseline(run.value.device_serial, props.id)
    await loadComparison()
  } catch (err) {
    error.value = err.message
  } finally {
    settingBaseline.value = false
  }
}

async function onDeleteRun() {
  if (!confirm(`Delete run ${props.id.slice(0, 8)}? This cannot be undone.`)) return
  error.value = ''
  deleting.value = true
  try {
    await deleteRun(props.id)
    router.push('/runs')
  } catch (err) {
    error.value = err.message
    deleting.value = false
  }
}

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, 2000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <p><router-link to="/runs">&larr; Back to runs</router-link></p>
  <p v-if="error" class="error">{{ error }}</p>

  <Card :title="`Run ${id.slice(0, 8)}`">
    <div v-if="run">
      <p>
        Status: <StatusBadge :label="run.status" :tone="STATUS_TONE[run.status] ?? 'neutral'" />
      </p>
      <p>Device: {{ run.device_serial }}</p>
      <p v-if="run.error" class="error">Error: {{ run.error }}</p>
      <button @click="onSetBaseline" :disabled="settingBaseline">
        {{ settingBaseline ? 'Setting…' : 'Set as baseline for this device' }}
      </button>
      <button
        v-if="run.status !== 'running' && run.status !== 'pending'"
        @click="onDeleteRun"
        :disabled="deleting"
      >
        {{ deleting ? 'Deleting…' : 'Delete this run' }}
      </button>
    </div>
  </Card>

  <Card title="Metrics">
    <p v-if="!metricNames.length">No samples yet.</p>
    <div v-else class="metric-grid">
      <MetricChart
        v-for="name in metricNames"
        :key="name"
        :name="name"
        :unit="samplesByMetric[name][samplesByMetric[name].length - 1]?.unit ?? ''"
        :samples="samplesByMetric[name]"
        :x-domain="xDomain"
        :baseline-mean="comparisonByMetric[name]?.baseline_mean ?? null"
        :regressed="comparisonByMetric[name]?.regressed ?? null"
      />
    </div>
    <details>
      <summary>Raw samples (last {{ RAW_TABLE_CAP }})</summary>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Collector</th>
            <th>Name</th>
            <th>Value</th>
            <th>Unit</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="sample in recentRaw.slice().reverse()" :key="sample.id">
            <td>{{ sample.timestamp }}</td>
            <td>{{ sample.collector }}</td>
            <td>{{ sample.name }}</td>
            <td>{{ sample.value }}</td>
            <td>{{ sample.unit }}</td>
          </tr>
        </tbody>
      </table>
    </details>
  </Card>

  <Card title="Comparison against baseline">
    <p v-if="!comparison">No baseline set for this device yet.</p>
    <div v-else>
      <p>
        Baseline run: {{ comparison.baseline_run_id.slice(0, 8) }} —
        <StatusBadge
          :label="comparison.regressed ? 'regression detected' : 'within threshold'"
          :tone="comparison.regressed ? 'danger' : 'success'"
        />
      </p>
      <table>
        <thead>
          <tr>
            <th>Metric</th>
            <th>Baseline mean</th>
            <th>Candidate mean</th>
            <th>Delta %</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in comparison.metrics" :key="metric.name">
            <td>{{ metric.name }}</td>
            <td>{{ metric.baseline_mean.toFixed(2) }}</td>
            <td>{{ metric.candidate_mean.toFixed(2) }}</td>
            <td>{{ metric.delta_pct === null ? '—' : metric.delta_pct.toFixed(1) + '%' }}</td>
            <td><DeltaBar :delta-pct="metric.delta_pct" :regressed="metric.regressed" /></td>
          </tr>
        </tbody>
      </table>
    </div>
  </Card>
</template>

<style scoped>
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
details {
  margin-top: var(--space-3);
}
details summary {
  cursor: pointer;
  color: var(--color-text-muted);
  font-size: 0.9em;
}
</style>
