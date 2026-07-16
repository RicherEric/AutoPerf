<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { listDevices, listRuns, listYoutubeScenarios, refreshDevices, triggerRun } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const STATUS_TONE = {
  completed: 'success',
  running: 'warning',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'danger',
}

const devices = ref([])
const runs = ref([])
const youtubeScenarios = ref([])
const selectedSerial = ref('')
const selectedScenario = ref('')
const duration = ref(60)
const error = ref('')
const refreshing = ref(false)
const starting = ref(false)

let pollHandle = null

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

onMounted(async () => {
  await Promise.all([loadDevices(), loadRuns(), listYoutubeScenarios().then((names) => (youtubeScenarios.value = names))])
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
          <option v-for="name in youtubeScenarios" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <button @click="onStartRun" :disabled="starting || !selectedSerial">
        {{ starting ? 'Starting…' : 'Start Run' }}
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
