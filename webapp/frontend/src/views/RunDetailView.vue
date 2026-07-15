<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { getComparison, getRun, listSamples, setBaseline } from '../api.js'

const props = defineProps({ id: String })

const run = ref(null)
const samples = ref([])
const comparison = ref(null)
const error = ref('')
const sinceId = ref(0)
const settingBaseline = ref(false)

let pollHandle = null
const TERMINAL_STATUSES = ['completed', 'failed', 'interrupted']

async function loadComparison() {
  comparison.value = await getComparison(props.id)
}

async function poll() {
  try {
    run.value = await getRun(props.id)
    const result = await listSamples(props.id, sinceId.value)
    samples.value = [...samples.value, ...result.samples].slice(-200)
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

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, 2000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <section>
    <p><router-link to="/">&larr; Back to runs</router-link></p>
    <h2>Run {{ id }}</h2>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="run">
      <p>Status: <strong>{{ run.status }}</strong></p>
      <p>Device: {{ run.device_serial }}</p>
      <p v-if="run.error" class="error">Error: {{ run.error }}</p>
      <button @click="onSetBaseline" :disabled="settingBaseline">
        {{ settingBaseline ? 'Setting…' : 'Set as baseline for this device' }}
      </button>
    </div>
  </section>

  <section>
    <h2>Comparison against baseline</h2>
    <p v-if="!comparison">No baseline set for this device yet.</p>
    <div v-else>
      <p>
        Baseline run: {{ comparison.baseline_run_id.slice(0, 8) }} —
        <strong :class="{ error: comparison.regressed }">
          {{ comparison.regressed ? 'regression detected' : 'within threshold' }}
        </strong>
      </p>
      <table>
        <thead>
          <tr>
            <th>Metric</th>
            <th>Baseline mean</th>
            <th>Candidate mean</th>
            <th>Delta %</th>
            <th>Regressed</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in comparison.metrics" :key="metric.name">
            <td>{{ metric.name }}</td>
            <td>{{ metric.baseline_mean.toFixed(2) }}</td>
            <td>{{ metric.candidate_mean.toFixed(2) }}</td>
            <td>{{ metric.delta_pct === null ? '—' : metric.delta_pct.toFixed(1) + '%' }}</td>
            <td :class="{ error: metric.regressed }">{{ metric.regressed ? 'yes' : 'no' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Recent metric samples</h2>
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
        <tr v-for="sample in samples.slice().reverse()" :key="sample.id">
          <td>{{ sample.timestamp }}</td>
          <td>{{ sample.collector }}</td>
          <td>{{ sample.name }}</td>
          <td>{{ sample.value }}</td>
          <td>{{ sample.unit }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
