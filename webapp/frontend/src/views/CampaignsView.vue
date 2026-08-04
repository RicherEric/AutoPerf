<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelCampaign,
  deleteCampaign,
  getCampaign,
  listCampaigns,
  listConnectedDevices,
  listYoutubeScenarios,
  triggerCampaign,
} from '../api.js'
import Card from '../components/Card.vue'
import PassRateBar from '../components/PassRateBar.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()

const STATUS_TONE = {
  completed: 'success',
  running: 'neutral',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'warning',
}

const devices = ref([])
const scenarios = ref([])
const campaigns = ref([])
const selectedId = ref('')
const detail = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)

const form = ref({
  kind: 'soak',
  serial: '',
  target: 'scenario',
  scenario: '',
  tier: 'regression',
  hours: 4,
  duration: 60,
  iterations: 20,
})

let pollHandle = null

// The one-button plan. A long run is the case where you have already decided
// what you want -- data, unattended, from whatever is plugged in -- so asking
// which device and which scenario is asking a question whose answer is always
// "all of them, the usual ones". Repeat over the smoke tier rather than a soak
// because a soak is one long run: cancel it and you keep nothing, whereas
// every completed iteration here is already a comparable sample.
//
// Sized for a night, and deliberately sized to *overrun* one. Finishing early
// wastes the rest of the night; finishing late costs nothing, because a
// campaign is cancellable and every iteration that completed is already
// saved. So the failure modes are not symmetric, and the number leans long.
const QUICK = { tier: 'smoke', iterations: 100, duration: 60 }

// `duration` is sampling time only. Around it each run also launches the app,
// waits for it to reach the foreground, verifies, then force-stops it -- and
// the scenarios that locate elements by identity pay for a UI dump on top.
// An allowance rather than a measurement, but leaving it out is what makes an
// estimate quietly optimistic, which for an overnight plan is the direction
// that matters.
const PER_RUN_OVERHEAD_SECONDS = 20

const tiers = computed(() => [...new Set(scenarios.value.map((s) => s.tier))])

const scenariosByTier = computed(() => {
  const grouped = {}
  for (const scenario of scenarios.value) {
    ;(grouped[scenario.tier] ||= []).push(scenario)
  }
  return grouped
})

// Mirrors services.plan_campaign_runs so the cost of a campaign is visible
// before it is submitted -- "repeat the regression tier 50 times" is 550 runs
// and several days of device time, which is not obvious from the inputs alone.
const plannedRunCount = computed(() => {
  if (form.value.kind === 'soak') return 1
  const perIteration = form.value.target === 'tier'
    ? (scenariosByTier.value[form.value.tier]?.length ?? 0)
    : 1
  return perIteration * Math.max(1, Number(form.value.iterations) || 0)
})

const estimatedSeconds = computed(() =>
  form.value.kind === 'soak'
    ? Number(form.value.hours) * 3600
    : plannedRunCount.value * Number(form.value.duration))

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.round((seconds % 3600) / 60)
  if (hours && minutes) return `${hours} h ${minutes} m`
  if (hours) return `${hours} h`
  return `${minutes} m`
}

function deviceLabel(serial) {
  const device = devices.value.find((d) => d.serial === serial)
  return device ? device.nickname || `${device.model} (${device.serial})` : serial
}

function campaignTarget(campaign) {
  return campaign.tier || campaign.scenario || t('common.noScenario')
}

function formatNumber(value) {
  if (value === null || value === undefined) return '—'
  return Math.abs(value) >= 1000
    ? Math.round(value).toLocaleString()
    : Number(value.toFixed(2)).toString()
}

function formatPct(value) {
  return value === null || value === undefined ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

// The regression threshold the backend used for this campaign's verdicts is
// reused as the drift alarm, so the page never states two different criteria.
function driftTone(trend) {
  if (trend.drift_pct === null || trend.drift_pct === undefined) return ''
  return Math.abs(trend.drift_pct) > (detail.value?.threshold_pct ?? 20) ? 'drift-alert' : ''
}

async function refresh() {
  try {
    campaigns.value = await listCampaigns()
    if (selectedId.value) {
      detail.value = await getCampaign(selectedId.value)
    }
    error.value = ''
  } catch (err) {
    error.value = err.message
  }
}

watch(selectedId, async (id) => {
  detail.value = null
  if (id) await refresh()
})

const quickRunsPerDevice = computed(() =>
  (scenariosByTier.value[QUICK.tier]?.length ?? 0) * QUICK.iterations)

const quickPlan = computed(() => ({
  devices: devices.value.length,
  runs: quickRunsPerDevice.value * devices.value.length,
  time: formatDuration(
    quickRunsPerDevice.value * (QUICK.duration + PER_RUN_OVERHEAD_SECONDS)),
}))

async function startQuick() {
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    // One campaign per device, because that is the shape the API and the
    // same-device run lock already have. They run interleaved, not queued
    // behind each other: Storage.try_start_run only excludes two jobs on the
    // *same* serial.
    const started = []
    const failed = []
    for (const device of devices.value) {
      try {
        const result = await triggerCampaign({
          kind: 'repeat',
          serial: device.serial,
          tier: QUICK.tier,
          iterations: QUICK.iterations,
          duration: QUICK.duration,
        })
        started.push(result)
      } catch (err) {
        failed.push(`${deviceLabel(device.serial)}: ${err.message}`)
      }
    }
    if (started.length) {
      notice.value = t('campaigns.quickStarted', {
        devices: started.length,
        count: started.reduce((total, r) => total + r.count, 0),
      })
      selectedId.value = started[0].campaign_id
    }
    // Partial success is reported as partial: a device that did not start is
    // a device that will have no data, and finding that out hours later is
    // the whole failure mode this page exists to avoid.
    if (failed.length) error.value = failed.join(' / ')
    await refresh()
  } finally {
    busy.value = false
  }
}

async function submit() {
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    const payload = {
      kind: form.value.kind,
      serial: form.value.serial,
      duration: form.value.kind === 'soak'
        ? Number(form.value.hours) * 3600
        : Number(form.value.duration),
    }
    if (form.value.kind === 'soak') {
      payload.scenario = form.value.scenario || null
    } else if (form.value.target === 'tier') {
      payload.tier = form.value.tier
      payload.iterations = Number(form.value.iterations)
    } else {
      payload.scenario = form.value.scenario || null
      payload.iterations = Number(form.value.iterations)
    }
    const result = await triggerCampaign(payload)
    notice.value = t('campaigns.started', { count: result.count })
    selectedId.value = result.campaign_id
    await refresh()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function cancel(campaignId) {
  if (!window.confirm(t('campaigns.cancelConfirm'))) return
  try {
    const result = await cancelCampaign(campaignId)
    notice.value = t('campaigns.cancelled', { count: result.cancelled_runs })
    await refresh()
  } catch (err) {
    error.value = err.message
  }
}

async function remove(campaignId) {
  if (!window.confirm(t('campaigns.deleteConfirm'))) return
  try {
    await deleteCampaign(campaignId)
    notice.value = t('campaigns.deleted')
    if (selectedId.value === campaignId) {
      selectedId.value = ''
      detail.value = null
    }
    await refresh()
  } catch (err) {
    error.value = err.message
  }
}

onMounted(async () => {
  try {
    ;[devices.value, scenarios.value] = await Promise.all([listConnectedDevices(), listYoutubeScenarios()])
    if (devices.value.length) form.value.serial = devices.value[0].serial
  } catch (err) {
    error.value = err.message
  }
  await refresh()
  pollHandle = setInterval(refresh, 5000)
})

onUnmounted(() => clearInterval(pollHandle))
</script>

<template>
  <Card :title="t('campaigns.title')">
    <p class="hint">{{ t('campaigns.intro') }}</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>
  </Card>

  <Card :title="t('campaigns.quickTitle')">
    <button class="quick-start" :disabled="busy || !devices.length" @click="startQuick">
      {{ busy ? t('campaigns.starting') : t('campaigns.quickButton') }}
    </button>
    <p v-if="devices.length" class="plan">
      {{ t('campaigns.quickPlan', {
        devices: quickPlan.devices, tier: QUICK.tier,
        iterations: QUICK.iterations, count: quickPlan.runs, time: quickPlan.time,
      }) }}
    </p>
    <p v-else class="hint">{{ t('campaigns.quickNoDevices') }}</p>
    <p class="hint">{{ t('campaigns.quickHint') }}</p>
  </Card>

  <Card :title="t('campaigns.createTitle')">
    <details>
      <summary class="advanced-summary">{{ t('campaigns.advancedSummary') }}</summary>
    <div class="form-grid">
      <label>
        {{ t('campaigns.kindLabel') }}
        <select v-model="form.kind">
          <option value="soak">{{ t('campaigns.kindSoak') }}</option>
          <option value="repeat">{{ t('campaigns.kindRepeat') }}</option>
        </select>
      </label>

      <label>
        {{ t('campaigns.deviceLabel') }}
        <select v-model="form.serial">
          <option v-for="d in devices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d.serial) }}
          </option>
        </select>
      </label>

      <label v-if="form.kind === 'repeat'">
        {{ t('campaigns.targetLabel') }}
        <select v-model="form.target">
          <option value="scenario">{{ t('campaigns.targetScenario') }}</option>
          <option value="tier">{{ t('campaigns.targetTier') }}</option>
        </select>
      </label>

      <label v-if="form.kind === 'repeat' && form.target === 'tier'">
        {{ t('campaigns.tierLabel') }}
        <select v-model="form.tier">
          <option v-for="tier in tiers" :key="tier" :value="tier">
            {{ tier }} ({{ scenariosByTier[tier]?.length ?? 0 }})
          </option>
        </select>
      </label>

      <label v-else>
        {{ t('campaigns.scenarioLabel') }}
        <select v-model="form.scenario">
          <option value="">{{ t('campaigns.noScenarioOption') }}</option>
          <optgroup v-for="tier in tiers" :key="tier" :label="tier">
            <option v-for="s in scenariosByTier[tier]" :key="s.name" :value="s.name" :title="s.description">
              {{ s.name }}
            </option>
          </optgroup>
        </select>
      </label>

      <label v-if="form.kind === 'soak'">
        {{ t('campaigns.hoursLabel') }}
        <input v-model.number="form.hours" type="number" min="0.1" step="0.5" />
      </label>

      <template v-else>
        <label>
          {{ t('campaigns.durationLabel') }}
          <input v-model.number="form.duration" type="number" min="5" step="5" />
        </label>
        <label>
          {{ t('campaigns.iterationsLabel') }}
          <input v-model.number="form.iterations" type="number" min="1" step="1" />
        </label>
      </template>
    </div>

    <p class="hint">
      {{ form.kind === 'soak' ? t('campaigns.kindSoakHint') : t('campaigns.kindRepeatHint') }}
    </p>
    <p class="plan">
      {{ t('campaigns.plannedRuns', { count: plannedRunCount }) }} ·
      {{ t('campaigns.estimatedTime', { time: formatDuration(estimatedSeconds) }) }}
    </p>
    <button :disabled="busy || !form.serial" @click="submit">
      {{ busy ? t('campaigns.starting') : t('campaigns.startButton') }}
    </button>
    </details>
  </Card>

  <Card :title="t('campaigns.listTitle')">
    <p v-if="!campaigns.length" class="hint">{{ t('campaigns.empty') }}</p>
    <table v-else>
      <thead>
        <tr>
          <th>{{ t('campaigns.colKind') }}</th>
          <th>{{ t('campaigns.colDevice') }}</th>
          <th>{{ t('campaigns.colTarget') }}</th>
          <th>{{ t('campaigns.colProgress') }}</th>
          <th>{{ t('campaigns.colStatus') }}</th>
          <th>{{ t('campaigns.colActions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in campaigns" :key="c.id" :class="{ 'row-selected': c.id === selectedId }">
          <td>{{ c.kind }}</td>
          <td>{{ deviceLabel(c.device_serial) }}</td>
          <td>{{ campaignTarget(c) }}</td>
          <td>
            <div class="progress-track">
              <div class="progress-fill" :style="{ width: `${c.progress_pct}%` }" />
            </div>
            <span class="progress-text">
              {{ t('campaigns.progressText', { finished: c.finished_count, total: c.run_count }) }}
            </span>
          </td>
          <td><StatusBadge :label="c.status" :tone="STATUS_TONE[c.status] || 'neutral'" /></td>
          <td class="actions">
            <button @click="selectedId = c.id">{{ t('campaigns.viewButton') }}</button>
            <button v-if="c.status === 'running'" @click="cancel(c.id)">{{ t('common.cancel') }}</button>
            <button @click="remove(c.id)">{{ t('common.delete') }}</button>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card v-if="detail" :title="t('campaigns.detailTitle', { id: detail.id.slice(0, 8) })">
    <p class="hint">{{ t('campaigns.thresholdHint', { threshold: detail.threshold_pct }) }}</p>

    <template v-if="detail.kind === 'repeat'">
      <h3>{{ t('campaigns.repeatTitle') }}</h3>
      <p class="hint">{{ t('campaigns.flakyExplain') }}</p>
      <div v-for="entry in detail.repeat.scenarios" :key="entry.scenario" class="scenario-block">
        <div class="scenario-head">
          <PassRateBar
            :label="entry.scenario || t('common.noScenario')"
            :pass-rate="entry.pass_rate === null ? null : entry.pass_rate / 100"
            :pass-count="entry.completed - entry.regressed"
            :fail-count="entry.errored + entry.regressed"
          />
          <StatusBadge v-if="entry.flaky" :label="t('campaigns.flakyBadge')" tone="warning" />
        </div>
        <p class="hint">
          <span v-if="entry.errored">{{ t('campaigns.errored', { count: entry.errored }) }}</span>
          <span v-if="entry.errored && entry.regressed"> · </span>
          <span v-if="entry.regressed">{{ t('campaigns.regressed', { count: entry.regressed }) }}</span>
        </p>
        <table v-if="entry.metric_stability.length" class="inner">
          <thead>
            <tr>
              <th>{{ t('campaigns.colMetric') }}</th>
              <th>{{ t('campaigns.colMean') }}</th>
              <th>{{ t('campaigns.colCv') }}</th>
              <th>{{ t('campaigns.colRange') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in entry.metric_stability" :key="m.name">
              <td>{{ m.name }}</td>
              <td>{{ formatNumber(m.mean) }}</td>
              <td>{{ m.cv_pct === null ? '—' : `${m.cv_pct.toFixed(1)}%` }}</td>
              <td>{{ formatNumber(m.minimum) }} – {{ formatNumber(m.maximum) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="hint">{{ t('campaigns.cvHint') }}</p>
    </template>

    <template v-else>
      <h3>{{ t('campaigns.soakTitle') }}</h3>
      <p v-if="!detail.soak.trends.length" class="hint">{{ t('campaigns.noSoakData') }}</p>
      <table v-else>
        <thead>
          <tr>
            <th>{{ t('campaigns.colMetric') }}</th>
            <th>{{ t('campaigns.colSlope') }}</th>
            <th>{{ t('campaigns.colStart') }}</th>
            <th>{{ t('campaigns.colEnd') }}</th>
            <th>{{ t('campaigns.colDrift') }}</th>
            <th>{{ t('campaigns.colSpan') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tr in detail.soak.trends" :key="tr.name">
            <td>{{ tr.name }}</td>
            <td>{{ formatNumber(tr.slope_per_hour) }}</td>
            <td>{{ formatNumber(tr.start_mean) }}</td>
            <td>{{ formatNumber(tr.end_mean) }}</td>
            <td :class="driftTone(tr)">{{ formatPct(tr.drift_pct) }}</td>
            <td>{{ t('campaigns.spanHours', { hours: tr.span_hours.toFixed(1) }) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">{{ t('campaigns.soakHint') }}</p>
      <p v-if="detail.soak.run_id">
        <router-link :to="`/runs/${detail.soak.run_id}`">{{ detail.soak.run_id.slice(0, 8) }}</router-link>
      </p>
    </template>
  </Card>

  <Card v-else-if="campaigns.length" :title="t('campaigns.detailTitle', { id: '—' })">
    <p class="hint">{{ t('campaigns.selectHint') }}</p>
  </Card>
</template>

<style scoped>
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  font-size: 0.85em;
}
.plan {
  font-size: 0.85em;
  font-weight: 600;
  margin: var(--space-2) 0 var(--space-3);
}
.quick-start {
  font-size: 1.05em;
  font-weight: 600;
  padding: var(--space-3) var(--space-4);
}
.advanced-summary {
  cursor: pointer;
  color: var(--color-text-muted);
  font-size: 0.85em;
  margin-bottom: var(--space-3);
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.notice {
  color: var(--color-success-text, var(--color-text));
  font-size: 0.9em;
}
.progress-track {
  height: 8px;
  background: var(--color-surface-alt);
  border-radius: 999px;
  overflow: hidden;
  min-width: 80px;
}
.progress-fill {
  height: 100%;
  background: var(--color-accent, var(--color-success-bg));
  border-radius: 999px;
}
.progress-text {
  font-size: 0.8em;
  color: var(--color-text-muted);
}
.row-selected {
  background: var(--color-surface-alt);
}
.actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.scenario-block {
  margin-bottom: var(--space-4);
}
.scenario-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
table.inner {
  font-size: 0.85em;
}
.drift-alert {
  color: var(--color-danger-text, crimson);
  font-weight: 600;
}
/* Wide tables must scroll inside their own container rather than pushing the
   page sideways -- the soak table has six numeric columns. */
table {
  display: block;
  overflow-x: auto;
  max-width: 100%;
}
</style>
