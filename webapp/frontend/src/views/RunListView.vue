<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelRun,
  connectDevice,
  deleteRun,
  cancelPreflight,
  getPreflight,
  getScenarioTargets,
  listConnectedDevices,
  listDevices,
  listPreflights,
  listRuns,
  listYoutubeScenarios,
  refreshDevices,
  setDeviceNickname,
  triggerPreflight,
  triggerRun,
  triggerSuite,
} from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DeviceMoodBadge from '../components/DeviceMoodBadge.vue'

const { t } = useI18n()

const STATUS_TONE = {
  completed: 'success',
  running: 'warning',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'danger',
}

// What triggered a run. All neutral except `unknown`, which is deliberately
// not: those are rows written before the column existed, and reading a trend
// without knowing whether it mixes attended and scheduled runs is the thing
// this column was added to stop. Fading them out would hide exactly that.
const ORIGIN_TONE = {
  manual: 'neutral',
  dashboard: 'neutral',
  campaign: 'neutral',
  suite: 'neutral',
  unknown: 'warning',
}

// Shallow to deep, mirroring smoke-test -> full regression-suite conventions:
// smoke only checks "does the app launch and reach basic content" (fast, run
// often); functional covers everyday interactions; regression covers
// deeper/edge-case flows meant for a slower cadence (e.g. nightly), precisely
// because they take longer and touch less-common paths.
const TIER_ORDER = ['smoke', 'functional', 'regression']

const devices = ref([])
// Two lists on purpose. The table below shows every device this database
// remembers, because its runs still name it. Every *picker* shows only what
// adb can see now, deduplicated: the same phone attached by USB and by WiFi
// is two serials with the same nickname, and picking the wrong "Galaxy A55"
// showed an empty history for a device that was running perfectly well.
const pickableDevices = ref([])
const runs = ref([])
const youtubeScenarios = ref([]) // [{ name, description, tier }]
const selectedSerial = ref('')
// The run history follows the device picker, because "the runs" almost always
// means "this device's runs" -- three devices' histories interleaved by time
// is not a list anyone reads. '' is the deliberate escape hatch (all devices),
// kept because comparing two devices is the other real question.
const runsFilterSerial = ref('')
const selectedScenario = ref('')
const duration = ref(60)
const error = ref('')
const refreshing = ref(false)
const starting = ref(false)
const startingSuite = ref('')
const cancellingId = ref('')
const connectAddress = ref('')
const connecting = ref(false)
const selectedIds = ref(new Set())
const bulkDeleting = ref(false)
// Which targets the selected scenario would have graded. null while unknown,
// [] when the scenario resolves none -- those two must not read the same.
const scenarioTargets = ref(null)
const preflightReport = ref(null)
const startingPreflight = ref(false)
const cancellingPreflight = ref(false)

let pollHandle = null

function deviceLabel(d) {
  const name = d.nickname || d.model
  const badge = d.connection === 'wifi' ? 'WiFi' : 'USB'
  return `${name} (${d.serial}) [${badge}]`
}

// The raw ISO timestamp (with microseconds + timezone offset, e.g.
// "2026-07-16T17:47:04.086985+00:00") is ~32 characters wide -- in a table
// column that alone was pushing the Actions column (Cancel/Delete) off the
// visible edge of the card. A short local-time format fits comfortably.
function formatTimestamp(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { hour12: false })
}

// Only a running run is undeletable. A queued one holds nothing, and while
// it was excluded here a queued campaign could not be cleared from the UI at
// all -- which is exactly when someone wants to.
// The scenario's own description, which is where "what does this test even
// do" is already written down -- one table, not a second copy in the UI.
function scenarioDescription(name) {
  return youtubeScenarios.value.find((s) => s.name === name)?.description ?? ''
}

function isDeletable(run) {
  return run.status !== 'running'
}

const deletableRuns = computed(() => runs.value.filter(isDeletable))
const allDeletableSelected = computed(() =>
  deletableRuns.value.length > 0 && deletableRuns.value.every((r) => selectedIds.value.has(r.id))
)

function toggleSelected(runId) {
  const next = new Set(selectedIds.value)
  if (next.has(runId)) {
    next.delete(runId)
  } else {
    next.add(runId)
  }
  selectedIds.value = next
}

function toggleSelectAll() {
  if (allDeletableSelected.value) {
    selectedIds.value = new Set()
  } else {
    selectedIds.value = new Set(deletableRuns.value.map((r) => r.id))
  }
}

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
  ;[devices.value, pickableDevices.value] = await Promise.all([listDevices(), listConnectedDevices()])
  if (!selectedSerial.value && pickableDevices.value.length) {
    selectedSerial.value = pickableDevices.value[0].serial
  }
}

async function loadRuns() {
  runs.value = await listRuns(runsFilterSerial.value)
  // A row that scrolled out of the filter must not stay selected: deleting
  // what you can no longer see is the one thing a filter must never do.
  const visible = new Set(runs.value.map((r) => r.id))
  selectedIds.value = new Set([...selectedIds.value].filter((id) => visible.has(id)))
}

async function onRefreshDevices() {
  error.value = ''
  refreshing.value = true
  try {
    devices.value = await refreshDevices()
    pickableDevices.value = await listConnectedDevices()
    if (!selectedSerial.value && pickableDevices.value.length) {
      selectedSerial.value = pickableDevices.value[0].serial
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

const preflightPossible = computed(() =>
  Boolean(selectedSerial.value) && (!selectedScenario.value || (scenarioTargets.value ?? []).length > 0)
)

const preflightBusy = computed(() =>
  ['pending', 'running'].includes(preflightReport.value?.status),
)

const preflightAttention = computed(() =>
  preflightReport.value?.summary?.needs_attention ?? [],
)

// Every target the check actually looked at, not only the ones with a
// problem. A report that lists nothing when everything passed cannot be told
// apart from a report that checked nothing -- and "which selector is holding
// this together, and by which strategy" is the question this whole mechanism
// exists to answer. `strategies` says how each one was found: content_desc is
// durable, resource_id survives only within an app version, structural means
// "the third clickable row", coordinates means it was not found at all.
const preflightTargets = computed(() => {
  const summary = preflightReport.value?.summary
  if (!summary) return []
  const rows = []
  for (const item of summary.needs_attention ?? []) {
    rows.push({ ...item, tone: 'danger', kind: 'attention' })
  }
  for (const item of summary.healthy ?? []) {
    rows.push({ ...item, tone: 'success', kind: 'healthy' })
  }
  for (const [key, tone] of [['by_design', 'neutral'], ['not_applicable', 'neutral'],
                             ['ungraded', 'warning']]) {
    for (const item of summary[key] ?? []) rows.push({ ...item, tone, kind: key })
  }
  return rows
})

async function loadScenarioTargets() {
  if (!selectedScenario.value) {
    scenarioTargets.value = null
    return
  }
  try {
    const { targets } = await getScenarioTargets(selectedScenario.value)
    scenarioTargets.value = targets
  } catch {
    // A failed lookup must not disable the button on a false premise; the
    // server refuses an unpreflightable scenario anyway.
    scenarioTargets.value = null
  }
}

async function refreshPreflight() {
  if (!selectedSerial.value) {
    preflightReport.value = null
    return
  }
  const [row] = await listPreflights(selectedSerial.value, 1)
  if (!row) {
    preflightReport.value = null
    return
  }
  // The list omits the summary, so the detail is fetched -- but only when
  // something actually changed, since this runs on the same 3s poll as the
  // run list.
  const current = preflightReport.value
  if (current?.id === row.id && current?.status === row.status) return
  preflightReport.value = await getPreflight(row.id)
}

async function onStartPreflight() {
  error.value = ''
  startingPreflight.value = true
  try {
    await triggerPreflight(selectedSerial.value, selectedScenario.value)
    await refreshPreflight()
  } catch (err) {
    error.value = err.message
  } finally {
    startingPreflight.value = false
  }
}

async function onCancelPreflight() {
  if (!preflightReport.value) return
  error.value = ''
  cancellingPreflight.value = true
  try {
    await cancelPreflight(preflightReport.value.id)
    await refreshPreflight()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingPreflight.value = false
  }
}

async function onBulkDelete() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  if (!confirm(t('runs.confirmBulkDelete', { count: ids.length }))) return
  error.value = ''
  bulkDeleting.value = true
  try {
    const results = await Promise.allSettled(ids.map((id) => deleteRun(id)))
    const failures = results.filter((r) => r.status === 'rejected')
    if (failures.length) {
      error.value = t('runs.bulkDeleteFailure', {
        failed: failures.length, total: ids.length, message: failures[0].reason.message,
      })
    }
    selectedIds.value = new Set()
    await loadRuns()
  } finally {
    bulkDeleting.value = false
  }
}

async function onCancelRun(runId) {
  error.value = ''
  cancellingId.value = runId
  try {
    await cancelRun(runId)
    await loadRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingId.value = ''
  }
}

async function onConnectDevice() {
  error.value = ''
  connecting.value = true
  try {
    const result = await connectDevice(connectAddress.value.trim())
    devices.value = result.devices
    connectAddress.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    connecting.value = false
  }
}

async function onSetNickname(serial, nickname) {
  error.value = ''
  try {
    await setDeviceNickname(serial, nickname)
    await loadDevices()
  } catch (err) {
    error.value = err.message
  }
}

watch(selectedScenario, loadScenarioTargets)
watch(selectedSerial, refreshPreflight)
// Picking a device to run on also narrows the history to that device. The
// filter stays independently settable afterwards, so choosing "all devices"
// is not undone by the next thing you do.
watch(selectedSerial, (serial) => {
  runsFilterSerial.value = serial
})
watch(runsFilterSerial, loadRuns)

onMounted(async () => {
  await Promise.all([
    loadDevices(),
    loadRuns(),
    listYoutubeScenarios().then((entries) => (youtubeScenarios.value = entries)),
  ])
  await refreshPreflight()
  pollHandle = setInterval(() => {
    loadRuns()
    // Only while it could still change: a finished report is not re-fetched.
    if (preflightBusy.value || !preflightReport.value) refreshPreflight()
  }, 3000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('runs.devicesTitle')">
    <p v-if="error" class="error">{{ error }}</p>
    <div class="connect-row">
      <input v-model="connectAddress" placeholder="192.168.1.50:5555" @keyup.enter="onConnectDevice" />
      <button @click="onConnectDevice" :disabled="connecting || !connectAddress.trim()">
        {{ connecting ? t('runs.connecting') : t('runs.connectButton') }}
      </button>
      <button @click="onRefreshDevices" :disabled="refreshing">
        {{ refreshing ? t('runs.refreshing') : t('runs.refreshButton') }}
      </button>
    </div>
    <table>
      <thead>
        <tr>
          <th>{{ t('runs.colNickname') }}</th>
          <th>{{ t('runs.colModel') }}</th>
          <th>{{ t('runs.colSerial') }}</th>
          <th>{{ t('runs.colConnection') }}</th>
          <th>{{ t('runs.colAndroid') }}</th>
          <th>{{ t('runs.colBattery') }}</th>
          <th></th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.serial">
          <td>
            <input
              class="nickname-input"
              :value="d.nickname ?? ''"
              :placeholder="t('common.unnamed')"
              @change="onSetNickname(d.serial, $event.target.value)"
            />
          </td>
          <td>{{ d.model }}</td>
          <td>{{ d.serial }}</td>
          <td>
            <StatusBadge
              :label="d.connection === 'wifi' ? 'WiFi' : 'USB'"
              :tone="d.connection === 'wifi' ? 'success' : 'neutral'"
            />
          </td>
          <td>{{ d.android_version ?? '—' }}</td>
          <td>{{ d.battery_level ?? '—' }}{{ d.battery_level != null ? '%' : '' }}</td>
          <td><DeviceMoodBadge :device="d" /></td>
          <td>
            <button v-if="d.serial !== selectedSerial" @click="selectedSerial = d.serial">{{ t('common.select') }}</button>
            <StatusBadge v-else :label="t('common.selected')" tone="success" />
          </td>
          <td>
            <details class="device-details">
              <summary>{{ t('common.moreInfo') }}</summary>
              <dl>
                <dt>{{ t('common.manufacturer') }}</dt><dd>{{ d.manufacturer ?? '—' }} / {{ d.brand ?? '—' }}</dd>
                <dt>{{ t('common.sdkVersion') }}</dt><dd>{{ d.sdk_version ?? '—' }}</dd>
                <dt>{{ t('common.buildId') }}</dt><dd>{{ d.build_id ?? '—' }}</dd>
                <dt>{{ t('common.cpuAbi') }}</dt><dd>{{ d.cpu_abi ?? '—' }}</dd>
                <dt>{{ t('common.chromeVersion') }}</dt><dd>{{ d.chrome_version ?? '—' }}</dd>
                <dt>{{ t('common.wifiIp') }}</dt><dd>{{ d.wifi_ip ?? '—' }}</dd>
                <dt>{{ t('common.userAgent') }}</dt><dd class="ua">{{ d.user_agent ?? '—' }}</dd>
              </dl>
            </details>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card :title="t('runs.startRunTitle')">
    <div>
      <label>
        {{ t('runs.deviceLabel') }}
        <select v-model="selectedSerial">
          <option v-for="d in pickableDevices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d) }}
          </option>
        </select>
      </label>
    </div>
    <div>
      <label>
        {{ t('runs.durationLabel') }}
        <input type="number" v-model="duration" min="1" />
      </label>
      <label>
        {{ t('runs.scenarioLabel') }}
        <select v-model="selectedScenario">
          <option value="">{{ t('runs.noneOption') }}</option>
          <optgroup v-for="tier in TIER_ORDER" :key="tier" :label="t(`device.tierLabels.${tier}`)">
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
        {{ starting ? t('runs.starting') : t('runs.startButton') }}
      </button>
    </div>
    <p v-if="selectedScenarioInfo" class="hint">{{ selectedScenarioInfo.description }}</p>
  </Card>

  <Card :title="t('runs.suiteTitle')">
    <p class="hint">{{ t('runs.suiteHint') }}</p>
    <div class="suite-buttons">
      <button
        v-for="tier in TIER_ORDER"
        :key="tier"
        @click="onRunSuite(tier)"
        :disabled="!!startingSuite || !selectedSerial"
      >
        {{ startingSuite === tier ? t('runs.starting') : t('runs.suiteButton', { tier, count: (scenariosByTier[tier] ?? []).length }) }}
      </button>
    </div>
  </Card>

  <Card :title="t('runs.preflight.title')">
    <p class="hint">{{ t('runs.preflight.hint') }}</p>
    <div class="preflight-row">
      <button @click="onStartPreflight" :disabled="startingPreflight || preflightBusy || !preflightPossible">
        {{ startingPreflight ? t('runs.preflight.starting') : t('runs.preflight.button') }}
      </button>
      <!-- A preflight holds the device for as long as it runs, and the whole
           covering set is minutes. Without this the only way out was to wait
           it out. It stops at the next scenario boundary, not instantly --
           the hint says so rather than letting the button imply otherwise. -->
      <button v-if="preflightBusy" @click="onCancelPreflight" :disabled="cancellingPreflight">
        {{ cancellingPreflight ? t('common.cancelling') : t('runs.preflight.cancelButton') }}
      </button>
      <span v-if="selectedScenario && (scenarioTargets ?? []).length" class="hint">
        {{ t('runs.preflight.scopedHint', { scenario: selectedScenario, count: scenarioTargets.length }) }}
      </span>
      <span v-else-if="selectedScenario && scenarioTargets" class="hint warn">
        {{ t('runs.preflight.noTargetsHint', { scenario: selectedScenario }) }}
      </span>
      <span v-else class="hint">{{ t('runs.preflight.fullHint') }}</span>
    </div>

    <p v-if="!preflightReport" class="hint">{{ t('runs.preflight.noReport') }}</p>
    <div v-else class="preflight-report">
      <div class="preflight-verdict">
        <StatusBadge
          :label="preflightReport.status"
          :tone="STATUS_TONE[preflightReport.status] ?? 'neutral'"
        />
        <StatusBadge
          v-if="preflightReport.ok === true"
          :label="t('runs.preflight.resultOk')"
          tone="success"
        />
        <StatusBadge
          v-else-if="preflightReport.ok === false"
          :label="t('runs.preflight.resultAttention', { count: preflightAttention.length })"
          tone="danger"
        />
        <span v-if="preflightReport.summary" class="hint">
          {{ t('runs.preflight.checkedSummary', {
            checked: preflightReport.summary.targets_checked,
            scenarios: (preflightReport.summary.scenarios_checked ?? []).join(', '),
          }) }}
        </span>
        <span v-if="preflightReport.app_version_name" class="hint">
          {{ t('runs.preflight.appVersion') }}: {{ preflightReport.app_version_name }}
        </span>
      </div>

      <p v-if="preflightBusy && cancellingPreflight" class="hint">
        {{ t('runs.preflight.cancelHint') }}
      </p>
      <p v-if="preflightReport.summary?.stopped_early" class="hint">
        {{ t('runs.preflight.stoppedEarly', {
          checked: (preflightReport.summary.scenarios_checked ?? []).length,
          planned: preflightReport.summary.scenarios_planned ?? 0,
        }) }}
      </p>
      <p v-if="preflightReport.error" class="error">
        {{ t('runs.preflight.failedLabel') }}: {{ preflightReport.error }}
      </p>

      <!-- Aggregated per target, because the fix is per target: one decayed
           selector shows up in several scenarios but is corrected in one place.
           Everything checked is listed, passing included: a report that shows
           nothing when all is well looks exactly like one that ran nothing. -->
      <table v-if="preflightTargets.length" class="preflight-targets">
        <thead>
          <tr>
            <th>{{ t('runs.preflight.colTarget') }}</th>
            <th>{{ t('runs.preflight.colResult') }}</th>
            <th>{{ t('runs.preflight.colHow') }}</th>
            <th>{{ t('runs.preflight.colScenarios') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in preflightTargets" :key="item.target + item.kind">
            <td><code>{{ item.target }}</code></td>
            <td>
              <StatusBadge :label="t(`runs.preflight.kind.${item.kind}`)" :tone="item.tone" />
            </td>
            <td class="how">
              <template v-if="(item.strategies ?? []).length">
                <code v-for="s in item.strategies" :key="s">{{ s }}</code>
              </template>
              <span v-else class="muted">{{ item.detail || '—' }}</span>
            </td>
            <td class="how">{{ (item.scenarios ?? []).join(', ') }}</td>
          </tr>
        </tbody>
      </table>

      <table v-if="preflightAttention.length">
        <thead>
          <tr>
            <th>{{ t('runs.preflight.colTarget') }}</th>
            <th>{{ t('runs.preflight.colStatus') }}</th>
            <th>{{ t('runs.preflight.colObserved') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in preflightAttention" :key="item.target">
            <td><code>{{ item.target }}</code></td>
            <td>{{ (item.status ?? []).join(', ') }}</td>
            <td class="observed">
              <!-- The report lists what was actually on screen so the right
                   selector is read off it rather than guessed at again. -->
              <code v-for="node in (item.observed_on_screen ?? []).slice(0, 6)"
                    :key="node.resource_id + node.text + node.content_desc">
                {{ node.content_desc || node.text || node.resource_id }}
              </code>
              <span v-if="!(item.observed_on_screen ?? []).length">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </Card>

  <Card :title="t('runs.runsTitle')">
    <div class="bulk-bar">
      <label class="runs-filter">
        {{ t('runs.filterDevice') }}
        <select v-model="runsFilterSerial">
          <option value="">{{ t('runs.filterAllDevices') }}</option>
          <option v-for="d in pickableDevices" :key="d.serial" :value="d.serial">
            {{ deviceLabel(d) }}
          </option>
        </select>
      </label>
      <button @click="onBulkDelete" :disabled="!selectedIds.size || bulkDeleting">
        {{ bulkDeleting ? t('common.deleting') : t('runs.bulkDeleteButton', { count: selectedIds.size }) }}
      </button>
    </div>
    <table class="runs-table">
      <thead>
        <tr>
          <th><input type="checkbox" :checked="allDeletableSelected" @change="toggleSelectAll" /></th>
          <th>{{ t('runs.colId') }}</th>
          <th>{{ t('runs.colDevice') }}</th>
          <th>{{ t('runs.colScenario') }}</th>
          <th>{{ t('runs.colOrigin') }}</th>
          <th>{{ t('runs.colStatus') }}</th>
          <th>{{ t('runs.colStarted') }}</th>
          <th>{{ t('runs.colFinished') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.id">
          <td>
            <input
              type="checkbox"
              :checked="selectedIds.has(run.id)"
              :disabled="!isDeletable(run)"
              @change="toggleSelected(run.id)"
            />
          </td>
          <td><router-link :to="`/runs/${run.id}`">{{ run.id.slice(0, 8) }}</router-link></td>
          <td>{{ run.device_serial }}</td>
          <!-- What this run was actually doing. A row that only says
               "completed" cannot be read: 24 presets do very different things
               to the phone, and the description is the difference between a
               name and a meaning. -->
          <td class="scenario">
            <span v-if="run.youtube_scenario" :title="scenarioDescription(run.youtube_scenario)">
              <code>{{ run.youtube_scenario }}</code>
              <em v-if="scenarioDescription(run.youtube_scenario)">
                {{ scenarioDescription(run.youtube_scenario) }}
              </em>
            </span>
            <span v-else class="muted">{{ t('common.noScenario') }}</span>
          </td>
          <td>
            <StatusBadge
              :label="t(`runs.origin.${run.origin || 'unknown'}`)"
              :tone="ORIGIN_TONE[run.origin] ?? 'neutral'"
            />
          </td>
          <td><StatusBadge :label="run.status" :tone="STATUS_TONE[run.status] ?? 'neutral'" /></td>
          <td>{{ formatTimestamp(run.started_at) }}</td>
          <td>{{ formatTimestamp(run.finished_at) }}</td>
          <td>
            <button
              v-if="run.status === 'running' || run.status === 'pending'"
              @click="onCancelRun(run.id)"
              :disabled="cancellingId === run.id"
            >
              {{ cancellingId === run.id ? '…' : t('common.cancel') }}
            </button>
          </td>
        </tr>
        <!-- An empty table right after filtering reads as a broken page.
             Say which of the two it is: nothing here yet, or nothing here
             for this device. -->
        <tr v-if="!runs.length">
          <td class="empty" colspan="9">
            {{ runsFilterSerial ? t('runs.noRunsForDevice') : t('runs.noRuns') }}
          </td>
        </tr>
      </tbody>
    </table>
  </Card>
</template>

<style scoped>
.runs-table td {
  white-space: nowrap;
}
.runs-table td.scenario {
  white-space: normal;
  max-width: 24rem;
}
.runs-table td.scenario em {
  display: block;
  font-style: normal;
  color: var(--color-text-muted);
  font-size: 0.8em;
  line-height: 1.35;
}
.runs-table td.scenario .muted {
  color: var(--color-text-muted);
}
.preflight-targets td.how {
  font-size: 0.85em;
  color: var(--color-text-muted);
}
.preflight-targets td.how code {
  margin-right: var(--space-1);
}
.runs-table td.empty {
  color: var(--color-text-muted);
  text-align: center;
  padding: var(--space-4) 0;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.suite-buttons {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.bulk-bar {
  margin-bottom: var(--space-3);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.runs-filter {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-muted);
  font-size: 0.9em;
}
.device-details summary {
  cursor: pointer;
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.device-details dl {
  margin: var(--space-2) 0 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--space-1) var(--space-3);
  max-width: 320px;
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
.connect-row {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-bottom: var(--space-3);
}
.connect-row input {
  flex: 1;
  max-width: 260px;
}
.nickname-input {
  width: 100%;
  max-width: 160px;
}
.preflight-row {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}
.preflight-verdict {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}
.preflight-report .observed code {
  margin-right: var(--space-2);
  white-space: nowrap;
}
.hint.warn {
  color: var(--color-warning, #8a6410);
}
</style>
