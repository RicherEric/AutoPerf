<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { getStats, listDevices } from '../api.js'
import Card from '../components/Card.vue'
import PassRateBar from '../components/PassRateBar.vue'
import MetricChart from '../components/MetricChart.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
// "unverified" is warning-toned rather than danger: the run did not fail,
// it failed to *measure* -- a broken test, not an observed regression.
const VERDICT_TONE = { pass: 'success', fail: 'danger', no_baseline: 'neutral', unverified: 'warning' }

const stats = ref(null)
const error = ref('')
const devices = ref([])
const selectedSerial = ref('') // '' = all devices combined
let pollHandle = null

function deviceLabel(d) {
  return d.nickname || `${d.model} (${d.serial})`
}

async function poll() {
  try {
    stats.value = await getStats(50, selectedSerial.value)
  } catch (err) {
    error.value = err.message
  }
}

watch(selectedSerial, poll)

// Worst-first so the page reads as "here's what needs attention" -- entries
// with no baseline yet (pass_rate === null) sort last since there's nothing
// actionable to show for them.
const sortedScenarios = computed(() => {
  if (!stats.value) return []
  return [...stats.value.by_scenario].sort((a, b) => {
    if (a.pass_rate === null && b.pass_rate === null) return 0
    if (a.pass_rate === null) return 1
    if (b.pass_rate === null) return -1
    return a.pass_rate - b.pass_rate
  })
})

const trendMetrics = computed(() => (stats.value ? Object.keys(stats.value.trend).sort() : []))

function trendXDomain(name) {
  const points = stats.value.trend[name]
  const times = points.map((p) => new Date(p.timestamp).getTime())
  return [Math.min(...times), Math.max(...times)]
}

// Which scenario is producing them. "17 unverified" is not actionable;
// "8 of them are search_and_play" is where to look.
const topUnverified = computed(() => {
  const rows = (stats.value?.by_scenario ?? []).filter((r) => r.unverified > 0)
  return rows.sort((a, b) => b.unverified - a.unverified)[0] ?? null
})

const overallPassRatePct = computed(() =>
  stats.value?.pass_rate === null || stats.value?.pass_rate === undefined
    ? '—'
    : `${Math.round(stats.value.pass_rate * 100)}%`
)

// Most-recent-first already from the backend; cap client-side for readability.
const recentRuns = computed(() => (stats.value ? stats.value.runs.slice(0, 20) : []))

function formatRegressedMetrics(metrics) {
  return metrics
    .map((m) => (m.delta_pct === null
      ? t('stats.deltaUnavailable', { name: m.name })
      : t('stats.deltaValue', { name: m.name, sign: m.delta_pct > 0 ? '+' : '', value: m.delta_pct.toFixed(0) })))
    .join('、')
}

onMounted(async () => {
  devices.value = await listDevices()
  await poll()
  pollHandle = setInterval(poll, 5000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('stats.title')">
    <p v-if="error" class="error">{{ error }}</p>
    <div class="device-filter">
      <label>
        {{ t('stats.deviceLabel') }}
        <select v-model="selectedSerial">
          <option value="">{{ t('stats.allDevices') }}</option>
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d) }}
          </option>
        </select>
      </label>
    </div>
    <div v-if="stats" class="stat-row">
      <div class="stat-tile">
        <span class="value">{{ overallPassRatePct }}</span>
        <span class="label">{{ t('stats.overallPassRate') }}</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ stats.runs_today }}</span>
        <span class="label">{{ t('stats.runsToday') }}</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ stats.total_runs }}</span>
        <span class="label">{{ t('stats.totalRuns') }}</span>
      </div>
      <!-- The count people actually look for. The old tile showed how many
           runs had *no* baseline, which is zero in the healthy case and reads
           as "no baselines exist". -->
      <div class="stat-tile">
        <span class="value">{{ stats.baselines }}</span>
        <span class="label">{{ t('stats.baselines') }}</span>
        <span class="note">{{ t('stats.baselinesNote') }}</span>
      </div>
      <!-- A bare count says nothing: 17 out of what, and is that bad? Shown
           as a fraction, with what it means and what it excludes. -->
      <div class="stat-tile" :class="{ 'stat-warn': stats.unverified > 0 }">
        <span class="value">
          {{ stats.unverified }}<span class="of">/ {{ stats.total_runs }}</span>
        </span>
        <span class="label">{{ t('stats.unverified') }}</span>
        <span class="note">{{ t('stats.unverifiedNote') }}</span>
      </div>
    </div>
    <!-- Where they came from, because the answer is always one or two
         scenarios and that is the thing worth acting on. -->
    <p v-if="stats && stats.no_baseline > 0" class="unverified-line">
      {{ t('stats.noBaselineLine', { count: stats.no_baseline }) }}
    </p>
    <p v-if="stats && stats.unverified > 0" class="unverified-line">
      {{ t('stats.unverifiedWhere', {
        count: stats.unverified,
        scenario: topUnverified?.scenario ?? '—',
        n: topUnverified?.unverified ?? 0,
      }) }}
    </p>
  </Card>

  <Card :title="t('stats.passRateTitle')">
    <p v-if="stats" class="hint">{{ t('stats.criteriaHint', { threshold: stats.threshold_pct }) }}</p>
    <p v-if="stats && !sortedScenarios.length" class="hint">{{ t('stats.noCompletedRuns') }}</p>
    <PassRateBar
      v-for="entry in sortedScenarios"
      :key="entry.scenario"
      :label="entry.scenario"
      :pass-rate="entry.pass_rate"
      :pass-count="entry.pass"
      :fail-count="entry.fail"
    />
  </Card>

  <Card :title="t('stats.recentVerdictsTitle')">
    <p v-if="stats && !recentRuns.length" class="hint">{{ t('stats.noCompletedRuns') }}</p>
    <p v-if="stats && stats.unverified > 0" class="hint">{{ t('stats.unverifiedHint') }}</p>
    <table v-else>
      <thead>
        <tr>
          <th>{{ t('stats.colRun') }}</th>
          <th>{{ t('stats.colScenario') }}</th>
          <th>{{ t('stats.colVerdict') }}</th>
          <th>{{ t('stats.colReason') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in recentRuns" :key="run.run_id">
          <td><router-link :to="`/runs/${run.run_id}`">{{ run.run_id.slice(0, 8) }}</router-link></td>
          <td>{{ run.scenario ?? t('common.noScenario') }}</td>
          <td><StatusBadge :label="t(`stats.verdict.${run.verdict}`)" :tone="VERDICT_TONE[run.verdict]" /></td>
          <td>
            <span v-if="run.verdict === 'fail'">{{ formatRegressedMetrics(run.regressed_metrics) }}</span>
            <span v-else-if="run.verdict === 'unverified'" class="warn">
              {{ t('stats.unverifiedReason', { count: run.quality?.verification_failures ?? 0 }) }}
            </span>
            <span v-else-if="run.verdict === 'no_baseline'" class="hint">{{ t('stats.noBaselineForDevice') }}</span>
            <span v-else class="hint">{{ t('stats.withinThreshold') }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card :title="t('stats.trendTitle')">
    <!-- An empty chart box reads as "no data". Say which of the two it is:
         nothing measured yet, or a question this scope cannot answer. -->
    <p v-if="stats && !trendMetrics.length" class="hint">
      {{ stats.trend_scope === 'all_devices'
          ? t('stats.trendNeedsOneDevice')
          : t('stats.notEnoughData') }}
    </p>
    <div class="metric-grid">
      <MetricChart
        v-for="name in trendMetrics"
        :key="name"
        :name="name"
        :samples="stats.trend[name]"
        :x-domain="trendXDomain(name)"
      />
    </div>
  </Card>
</template>

<style scoped>
.stat-tile .note {
  display: block;
  color: var(--color-text-muted);
  font-size: 0.72em;
  line-height: 1.3;
  margin-top: 0.2em;
}
.stat-tile .of {
  font-size: 0.5em;
  color: var(--color-text-muted);
  margin-left: 0.2em;
}
.unverified-line {
  margin-top: var(--space-3);
  font-size: 0.88em;
  color: var(--color-text-muted);
}
.device-filter {
  margin-bottom: var(--space-4);
}
.stat-row {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
}
.stat-row .stat-tile {
  flex: 1;
  min-width: 140px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-4);
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.warn {
  color: var(--color-warning-text, var(--color-text));
  font-size: 0.85em;
}
.stat-warn .value {
  color: var(--color-warning-text, var(--color-text));
}
</style>
