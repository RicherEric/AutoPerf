<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { cancelRun, deleteRun, getComparison, getRun, listDevices, listSamples, setBaseline } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DeltaBar from '../components/DeltaBar.vue'
import MetricChart from '../components/MetricChart.vue'

const props = defineProps({ id: String })
const router = useRouter()
const { t } = useI18n()

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
const device = ref(null)
const comparison = ref(null)
const error = ref('')
const sinceId = ref(0)
const settingBaseline = ref(false)
const deleting = ref(false)
const cancelling = ref(false)

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
    if (run.value && !device.value) {
      const devices = await listDevices()
      device.value = devices.find((d) => d.serial === run.value.device_serial) ?? null
    }
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

async function onCancelRun() {
  error.value = ''
  cancelling.value = true
  try {
    await cancelRun(props.id)
    await poll()
  } catch (err) {
    error.value = err.message
  } finally {
    cancelling.value = false
  }
}

async function onDeleteRun() {
  if (!confirm(t('runDetail.confirmDelete', { id: props.id.slice(0, 8) }))) return
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
  <p><router-link to="/runs">{{ t('runDetail.backLink') }}</router-link></p>
  <p v-if="error" class="error">{{ error }}</p>

  <Card :title="t('runDetail.runTitle', { id: id.slice(0, 8) })">
    <div v-if="run">
      <p>
        {{ t('runDetail.statusLabel') }} <StatusBadge :label="run.status" :tone="STATUS_TONE[run.status] ?? 'neutral'" />
      </p>
      <p>
        {{ t('runDetail.deviceLabel') }} {{ device?.nickname || device?.model || run.device_serial }} ({{ run.device_serial }})
      </p>
      <details v-if="device" class="device-details">
        <summary>{{ t('common.moreInfo') }}</summary>
        <dl>
          <dt>{{ t('common.manufacturer') }}</dt><dd>{{ device.manufacturer ?? '—' }} / {{ device.brand ?? '—' }}</dd>
          <dt>Android</dt><dd>{{ device.android_version ?? '—' }}(SDK {{ device.sdk_version ?? '—' }})</dd>
          <dt>{{ t('common.buildId') }}</dt><dd>{{ device.build_id ?? '—' }}</dd>
          <dt>{{ t('common.cpuAbi') }}</dt><dd>{{ device.cpu_abi ?? '—' }}</dd>
          <dt>{{ t('common.connection') }}</dt><dd>{{ device.connection === 'wifi' ? 'WiFi' : 'USB' }}</dd>
          <dt>{{ t('common.battery') }}</dt><dd>{{ device.battery_level ?? '—' }}{{ device.battery_level != null ? '%' : '' }}</dd>
          <dt>{{ t('common.chromeVersion') }}</dt><dd>{{ device.chrome_version ?? '—' }}</dd>
          <dt>{{ t('common.wifiIp') }}</dt><dd>{{ device.wifi_ip ?? '—' }}</dd>
          <dt>{{ t('common.userAgent') }}</dt><dd class="ua">{{ device.user_agent ?? '—' }}</dd>
        </dl>
      </details>
      <p v-if="run.error" class="error">{{ t('runDetail.errorLabel') }} {{ run.error }}</p>
      <button @click="onSetBaseline" :disabled="settingBaseline">
        {{ settingBaseline ? t('runDetail.settingBaseline') : t('runDetail.setBaselineButton', { scenario: run.youtube_scenario || t('common.noScenario') }) }}
      </button>
      <p class="hint">
        {{ t('runDetail.baselineHint', {
          scenarioStrong: t('runDetail.baselineHintScenarioStrong'),
          scenario: run.youtube_scenario || t('common.noScenario'),
          scopeStrong: t('runDetail.baselineHintScopeStrong'),
        }) }}
      </p>
      <button
        v-if="run.status === 'running' || run.status === 'pending'"
        @click="onCancelRun"
        :disabled="cancelling || run.cancel_requested"
      >
        {{ (run.cancel_requested || cancelling) ? t('common.cancelling') : t('runDetail.cancelButton') }}
      </button>
      <button v-else @click="onDeleteRun" :disabled="deleting">
        {{ deleting ? t('common.deleting') : t('runDetail.deleteButton') }}
      </button>
    </div>
  </Card>

  <Card :title="t('runDetail.metricsTitle')">
    <p v-if="!metricNames.length">{{ t('runDetail.noSamplesYet') }}</p>
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
      <summary>{{ t('runDetail.rawSamplesSummary', { count: RAW_TABLE_CAP }) }}</summary>
      <table>
        <thead>
          <tr>
            <th>{{ t('runDetail.colTime') }}</th>
            <th>{{ t('runDetail.colCollector') }}</th>
            <th>{{ t('runDetail.colName') }}</th>
            <th>{{ t('runDetail.colValue') }}</th>
            <th>{{ t('runDetail.colUnit') }}</th>
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

  <Card :title="t('runDetail.comparisonTitle')">
    <p v-if="!comparison">{{ t('runDetail.noBaselineYet') }}</p>
    <div v-else>
      <p>
        {{ t('runDetail.baselineRunLabel') }} {{ comparison.baseline_run_id.slice(0, 8) }} —
        <StatusBadge
          :label="comparison.regressed ? t('runDetail.regressionDetected') : t('runDetail.withinThreshold')"
          :tone="comparison.regressed ? 'danger' : 'success'"
        />
      </p>
      <table>
        <thead>
          <tr>
            <th>{{ t('runDetail.colMetric') }}</th>
            <th>{{ t('runDetail.colBaselineMean') }}</th>
            <th>{{ t('runDetail.colCandidateMean') }}</th>
            <th>{{ t('runDetail.colDeltaPct') }}</th>
            <th>{{ t('runDetail.colChange') }}</th>
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
.device-details dl {
  margin: var(--space-2) 0 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--space-1) var(--space-3);
  max-width: 480px;
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
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
  max-width: 42em;
}
</style>
