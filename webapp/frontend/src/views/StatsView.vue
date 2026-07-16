<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getStats } from '../api.js'
import Card from '../components/Card.vue'
import PassRateBar from '../components/PassRateBar.vue'
import MetricChart from '../components/MetricChart.vue'
import StatusBadge from '../components/StatusBadge.vue'

const VERDICT_TONE = { pass: 'success', fail: 'danger', no_baseline: 'neutral' }
const VERDICT_LABEL = { pass: 'pass', fail: 'fail', no_baseline: '無 baseline' }

const stats = ref(null)
const error = ref('')
let pollHandle = null

async function poll() {
  try {
    stats.value = await getStats()
  } catch (err) {
    error.value = err.message
  }
}

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

const overallPassRatePct = computed(() =>
  stats.value?.pass_rate === null || stats.value?.pass_rate === undefined
    ? '—'
    : `${Math.round(stats.value.pass_rate * 100)}%`
)

// Most-recent-first already from the backend; cap client-side for readability.
const recentRuns = computed(() => (stats.value ? stats.value.runs.slice(0, 20) : []))

function formatRegressedMetrics(metrics) {
  return metrics
    .map((m) => (m.delta_pct === null ? `${m.name}（無法計算差異）` : `${m.name} ${m.delta_pct > 0 ? '+' : ''}${m.delta_pct.toFixed(0)}%`))
    .join('、')
}

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, 5000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card title="統計概覽">
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="stats" class="stat-row">
      <div class="stat-tile">
        <span class="value">{{ overallPassRatePct }}</span>
        <span class="label">整體通過率</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ stats.runs_today }}</span>
        <span class="label">今日執行次數</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ stats.total_runs }}</span>
        <span class="label">近期總 Run 數</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ stats.no_baseline }}</span>
        <span class="label">尚無 Baseline</span>
      </div>
    </div>
  </Card>

  <Card title="每個腳本的通過率">
    <p v-if="stats" class="hint">
      判定標準:與該裝置的 baseline 相比,任一項 metric(CPU/記憶體/電量/溫度)的平均值變動超過
      {{ stats.threshold_pct }}%,這個 run 就判定為 fail(不分變高或變低,見下方「近期判定明細」)。
      還沒設定 baseline 的裝置,run 會顯示「無 baseline」,不計入通過率。
    </p>
    <p v-if="stats && !sortedScenarios.length" class="hint">還沒有任何完成的 run。</p>
    <PassRateBar
      v-for="entry in sortedScenarios"
      :key="entry.scenario"
      :label="entry.scenario"
      :pass-rate="entry.pass_rate"
      :pass-count="entry.pass"
      :fail-count="entry.fail"
    />
  </Card>

  <Card title="近期判定明細">
    <p v-if="stats && !recentRuns.length" class="hint">還沒有任何完成的 run。</p>
    <table v-else>
      <thead>
        <tr>
          <th>Run</th>
          <th>腳本</th>
          <th>判定</th>
          <th>原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in recentRuns" :key="run.run_id">
          <td><router-link :to="`/runs/${run.run_id}`">{{ run.run_id.slice(0, 8) }}</router-link></td>
          <td>{{ run.scenario ?? '(no scenario)' }}</td>
          <td><StatusBadge :label="VERDICT_LABEL[run.verdict]" :tone="VERDICT_TONE[run.verdict]" /></td>
          <td>
            <span v-if="run.verdict === 'fail'">{{ formatRegressedMetrics(run.regressed_metrics) }}</span>
            <span v-else-if="run.verdict === 'no_baseline'" class="hint">此裝置尚未設定 baseline</span>
            <span v-else class="hint">全部 metric 都在門檻內</span>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card title="最近趨勢（各 Metric 隨 Run 歷史的平均值）">
    <p v-if="stats && !trendMetrics.length" class="hint">還沒有足夠的資料。</p>
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
</style>
