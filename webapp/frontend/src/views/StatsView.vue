<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getStats } from '../api.js'
import Card from '../components/Card.vue'
import PassRateBar from '../components/PassRateBar.vue'
import MetricChart from '../components/MetricChart.vue'

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
