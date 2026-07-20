<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { getQueueStatus, listDevices, listSamples } from '../api.js'
import Card from '../components/Card.vue'
import LiveScreenPanel from '../components/LiveScreenPanel.vue'
import MiniSparkline from '../components/MiniSparkline.vue'

const { t } = useI18n()

const POLL_MS = 3000
const SPARKLINE_METRIC = 'cpu.total'
const SPARKLINE_CAP = 30

const runningRuns = ref([])
const devicesBySerial = reactive({})
const samplesByRun = reactive({})
let pollHandle = null

function deviceLabel(serial) {
  const d = devicesBySerial[serial]
  return d ? (d.nickname || d.model) : serial
}

async function poll() {
  const [status, devices] = await Promise.all([getQueueStatus(), listDevices()])
  runningRuns.value = status.running_runs
  for (const d of devices) {
    devicesBySerial[d.serial] = d
  }
  await Promise.all(
    runningRuns.value.map(async (run) => {
      const result = await listSamples(run.id)
      const cpuSamples = result.samples.filter((s) => s.name === SPARKLINE_METRIC)
      samplesByRun[run.id] = cpuSamples.slice(-SPARKLINE_CAP)
    })
  )
}

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, POLL_MS)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('missionControl.title')">
    <p class="hint">{{ t('missionControl.hint') }}</p>
    <p v-if="!runningRuns.length">{{ t('missionControl.noneRunning') }}</p>
    <div v-else class="mc-grid">
      <div v-for="run in runningRuns" :key="run.id" class="mc-cell">
        <div class="mc-cell-title">
          <router-link :to="`/runs/${run.id}`">{{ deviceLabel(run.device_serial) }}</router-link>
        </div>
        <LiveScreenPanel :serial="run.device_serial" :active="true" :run-id="run.id" />
        <MiniSparkline :samples="samplesByRun[run.id] ?? []" />
      </div>
    </div>
  </Card>
</template>

<style scoped>
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.mc-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-4);
}
.mc-cell {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--color-surface);
}
.mc-cell-title {
  font-weight: 600;
  margin-bottom: var(--space-2);
}
</style>
