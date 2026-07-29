<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelRun,
  connectDiscoveredDevices,
  connectDevice,
  discoverMdnsDevices,
  listDevices,
  listRuns,
  listYoutubeScenarios,
  pairDevice,
  refreshDevices,
  setDeviceNickname,
  triggerRun,
  triggerSuite,
} from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
const STATUS_TONE = {
  completed: 'success',
  running: 'warning',
  pending: 'neutral',
  failed: 'danger',
  interrupted: 'danger',
}

const TIER_ORDER = ['smoke', 'functional', 'regression']

const devices = ref([])
const runs = ref([])
const error = ref('')
const info = ref('')

// Most-recent pending/running run per device, so "Start test" can't fire a
// second concurrent run for a phone that already has one in flight/queued.
const activeRunBySerial = computed(() => {
  const map = {}
  for (const run of runs.value) {
    if ((run.status === 'running' || run.status === 'pending') && !map[run.device_serial]) {
      map[run.device_serial] = run
    }
  }
  return map
})

const selectableDevices = computed(() =>
  devices.value.filter((device) => !activeRunBySerial.value[device.serial]),
)
const selectedDevices = computed(() => {
  const selected = new Set(selectedSerials.value)
  return selectableDevices.value.filter((device) => selected.has(device.serial))
})
const allSelectableSelected = computed(() =>
  selectableDevices.value.length > 0
  && selectedDevices.value.length === selectableDevices.value.length,
)

const pairAddress = ref('')
const pairCode = ref('')
const pairing = ref(false)

const connectAddress = ref('')
const connecting = ref(false)
const mdnsServices = ref([])
const discovering = ref(false)
const connectingDiscovered = ref(false)
const connectingAddress = ref('')
const mdnsResultByAddress = ref({})
const mdnsDevices = computed(() => {
  const grouped = new Map()
  for (const service of mdnsServices.value) {
    const id = service.device_id || service.name
    const device = grouped.get(id) || { id, name: service.name, pairing: null, connect: null }
    if (service.kind === 'pairing') device.pairing = service
    if (service.kind === 'connect') device.connect = service
    grouped.set(id, device)
  }
  return [...grouped.values()]
})

const startingSerial = ref('')
const cancellingSerial = ref('')
const selectedSerials = ref([])
const startingSelected = ref(false)
const scenarios = ref([])
const selectedScenario = ref('device_settings_scroll')
const startingSelectedSuite = ref('')
const scenariosByTier = computed(() => {
  const grouped = {}
  for (const scenario of scenarios.value) {
    if (!grouped[scenario.tier]) grouped[scenario.tier] = []
    grouped[scenario.tier].push(scenario)
  }
  return grouped
})

let pollHandle = null

async function loadDevices() {
  devices.value = await refreshDevices()
}

async function onPair() {
  error.value = ''
  info.value = ''
  pairing.value = true
  try {
    const result = await pairDevice(pairAddress.value.trim(), pairCode.value.trim())
    info.value = t('join.pairSuccess', { message: result.message })
    pairAddress.value = ''
    pairCode.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    pairing.value = false
  }
}

async function onConnect() {
  error.value = ''
  info.value = ''
  connecting.value = true
  try {
    const result = await connectDevice(connectAddress.value.trim())
    devices.value = result.devices
    info.value = t('join.connectSuccess', { message: result.message })
    connectAddress.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    connecting.value = false
  }
}

async function onConnectService(address) {
  error.value = ''
  info.value = ''
  connectingAddress.value = address
  try {
    const result = await connectDevice(address)
    devices.value = result.devices
    mdnsResultByAddress.value[address] = { ok: true, message: result.message }
  } catch (err) {
    mdnsResultByAddress.value[address] = { ok: false, message: err.message }
  } finally {
    connectingAddress.value = ''
  }
}

async function onDiscover() {
  error.value = ''
  discovering.value = true
  try {
    const result = await discoverMdnsDevices()
    mdnsServices.value = result.services
    const pairingService = result.services.find((service) => service.kind === 'pairing')
    if (pairingService) pairAddress.value = pairingService.address
    const connectService = result.services.find((service) => service.kind === 'connect')
    if (connectService) connectAddress.value = connectService.address
  } catch (err) {
    error.value = err.message
  } finally {
    discovering.value = false
  }
}

function usePairingService(service) {
  pairAddress.value = service.address
  pairCode.value = ''
  document.querySelector('.pairing-form input')?.focus()
}

async function onConnectDiscovered() {
  error.value = ''
  info.value = ''
  connectingDiscovered.value = true
  try {
    const result = await connectDiscoveredDevices()
    mdnsServices.value = result.services
    devices.value = result.devices
    mdnsResultByAddress.value = Object.fromEntries(
      result.results.map((item) => [
        item.address,
        { ok: item.ok, message: item.message || item.error },
      ]),
    )
    const connected = result.results.filter((item) => item.ok).length
    const failed = result.results.length - connected
    const summary = t('join.mdnsConnectSummary', { connected, failed })
    if (failed) error.value = summary
    else info.value = summary
  } catch (err) {
    error.value = err.message
  } finally {
    connectingDiscovered.value = false
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

async function onStartTest(serial) {
  error.value = ''
  startingSerial.value = serial
  try {
    await triggerRun(serial, 30, selectedScenario.value)
    runs.value = await listRuns()
    info.value = t('join.testStarted', { device: serial, scenario: selectedScenario.value })
  } catch (err) {
    error.value = err.message
  } finally {
    startingSerial.value = ''
  }
}

function toggleSelectAll() {
  selectedSerials.value = allSelectableSelected.value
    ? []
    : selectableDevices.value.map((device) => device.serial)
}

async function onStartSelected() {
  error.value = ''
  info.value = ''
  const serials = selectedDevices.value.map((device) => device.serial)
  if (!serials.length) return

  startingSelected.value = true
  try {
    const results = await Promise.allSettled(
      serials.map((serial) => triggerRun(serial, 30, selectedScenario.value)),
    )
    const started = results.filter((result) => result.status === 'fulfilled').length
    const failed = results.length - started
    selectedSerials.value = []
    runs.value = await listRuns()
    const summary = t('join.bulkStartSummary', { started, failed })
    if (failed) error.value = summary
    else info.value = summary
  } finally {
    startingSelected.value = false
  }
}

async function onStartSelectedSuite(tier) {
  error.value = ''
  info.value = ''
  const serials = selectedDevices.value.map((device) => device.serial)
  if (!serials.length) return

  startingSelectedSuite.value = tier
  try {
    const results = await Promise.allSettled(
      serials.map((serial) => triggerSuite(serial, tier, 30)),
    )
    const started = results.filter((result) => result.status === 'fulfilled').length
    const failed = results.length - started
    selectedSerials.value = []
    runs.value = await listRuns()
    const summary = t('join.bulkSuiteSummary', { tier, started, failed })
    if (failed) error.value = summary
    else info.value = summary
  } finally {
    startingSelectedSuite.value = ''
  }
}

async function onCancelRun(runId, serial) {
  error.value = ''
  cancellingSerial.value = serial
  try {
    await cancelRun(runId)
    runs.value = await listRuns()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingSerial.value = ''
  }
}

async function poll() {
  try {
    ;[devices.value, runs.value] = await Promise.all([listDevices(), listRuns()])
  } catch {
    // transient poll failure -- next tick retries
  }
}

onMounted(async () => {
  await Promise.all([
    poll(),
    listYoutubeScenarios().then((result) => { scenarios.value = result }),
  ])
  pollHandle = setInterval(poll, 3000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('join.title')">
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>
    <p class="hint">{{ t('join.intro') }}</p>
    <ol class="steps">
      <li>{{ t('join.step1') }}</li>
      <li>{{ t('join.step2') }}</li>
      <li>{{ t('join.step3') }}</li>
      <li>{{ t('join.step4') }}</li>
      <li>{{ t('join.step5') }}</li>
      <li>{{ t('join.step6') }}</li>
    </ol>
  </Card>

  <Card :title="t('join.pairSectionTitle')">
    <div class="form-row pairing-form">
      <input v-model="pairAddress" :placeholder="t('join.pairAddressPlaceholder')" />
      <input v-model="pairCode" :placeholder="t('join.pairCodePlaceholder')" maxlength="6" />
      <button @click="onPair" :disabled="pairing || !pairAddress.trim() || !pairCode.trim()">
        {{ pairing ? t('join.pairing') : t('join.pairButton') }}
      </button>
    </div>
  </Card>

  <Card :title="t('join.connectSectionTitle')">
    <div class="form-row">
      <input v-model="connectAddress" :placeholder="t('join.connectAddressPlaceholder')" />
      <button @click="onConnect" :disabled="connecting || !connectAddress.trim()">
        {{ connecting ? t('join.connecting') : t('join.connectButton') }}
      </button>
    </div>
  </Card>

  <Card :title="t('join.mdnsSectionTitle')">
    <p class="hint">{{ t('join.mdnsHint') }}</p>
    <div class="form-row">
      <button @click="onDiscover" :disabled="discovering || connectingDiscovered">
        {{ discovering ? t('join.mdnsScanning') : t('join.mdnsScanButton') }}
      </button>
      <button
        @click="onConnectDiscovered"
        :disabled="connectingDiscovered || discovering || !mdnsServices.some((service) => service.kind === 'connect')"
      >
        {{ connectingDiscovered ? t('join.connecting') : t('join.mdnsConnectAllButton') }}
      </button>
    </div>
    <p v-if="!mdnsServices.length && !discovering" class="hint">{{ t('join.mdnsNone') }}</p>
    <table v-else-if="mdnsDevices.length" class="mdns-table">
      <thead>
        <tr>
          <th>{{ t('join.mdnsName') }}</th>
          <th>{{ t('join.mdnsAddress') }}</th>
          <th>{{ t('join.mdnsStatus') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="device in mdnsDevices" :key="device.id">
          <td>{{ device.id }}</td>
          <td>
            <div v-if="device.connect">
              <span class="endpoint-label">{{ t('join.mdnsConnectAddress') }}</span>
              <code>{{ device.connect.address }}</code>
            </div>
            <div v-if="device.pairing">
              <span class="endpoint-label">{{ t('join.mdnsPairAddress') }}</span>
              <code>{{ device.pairing.address }}</code>
            </div>
          </td>
          <td>
            <StatusBadge
              v-if="device.connect"
              :label="t('join.mdnsEndpoint')"
              tone="success"
            />
            <StatusBadge
              v-if="device.pairing"
              :label="t('join.mdnsNeedsPairing')"
              tone="warning"
            />
            <p
              v-if="device.connect && mdnsResultByAddress[device.connect.address]"
              :class="mdnsResultByAddress[device.connect.address].ok ? 'result-success' : 'result-error'"
            >
              {{
                mdnsResultByAddress[device.connect.address].ok
                  ? mdnsResultByAddress[device.connect.address].message
                  : t('join.mdnsConnectFailed', { error: mdnsResultByAddress[device.connect.address].message })
              }}
            </p>
          </td>
          <td>
            <button
              v-if="device.connect"
              @click="onConnectService(device.connect.address)"
              :disabled="connecting || connectingDiscovered"
            >
              {{ connectingAddress === device.connect.address ? t('join.connecting') : t('join.connectButton') }}
            </button>
            <button
              v-if="device.pairing"
              @click="usePairingService(device.pairing)"
            >
              {{ t('join.mdnsUsePairingButton') }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card :title="t('join.joinedSectionTitle')">
    <p v-if="!devices.length" class="hint">{{ t('join.noDevicesYet') }}</p>
    <template v-else>
      <div class="bulk-actions">
        <label class="scenario-picker">
          <span>{{ t('join.scenarioLabel') }}</span>
          <select v-model="selectedScenario" :disabled="startingSelected">
            <optgroup v-for="tier in TIER_ORDER" :key="tier" :label="t(`device.tierLabels.${tier}`)">
              <option
                v-for="scenario in scenariosByTier[tier] ?? []"
                :key="scenario.name"
                :value="scenario.name"
              >
                {{ scenario.name }} — {{ scenario.description }}
              </option>
            </optgroup>
          </select>
        </label>
        <button @click="toggleSelectAll" :disabled="!selectableDevices.length || startingSelected">
          {{ allSelectableSelected ? t('join.clearSelectionButton') : t('join.selectAllButton') }}
        </button>
        <button @click="onStartSelected" :disabled="!selectedDevices.length || startingSelected">
          {{
            startingSelected
              ? t('join.starting')
              : t('join.startSelectedButton', { count: selectedDevices.length })
          }}
        </button>
      </div>
      <div class="suite-actions">
        <span>{{ t('join.multiSuiteLabel') }}</span>
        <button
          v-for="tier in TIER_ORDER"
          :key="tier"
          @click="onStartSelectedSuite(tier)"
          :disabled="!selectedDevices.length || Boolean(startingSelectedSuite) || startingSelected"
        >
          {{
            startingSelectedSuite === tier
              ? t('join.starting')
              : t('join.startSelectedSuiteButton', {
                  tier,
                  devices: selectedDevices.length,
                  scenarios: (scenariosByTier[tier] ?? []).length,
                })
          }}
        </button>
      </div>
      <table>
      <thead>
        <tr>
          <th>{{ t('join.colSelect') }}</th>
          <th>{{ t('join.colNickname') }}</th>
          <th>{{ t('join.colModel') }}</th>
          <th>{{ t('join.colConnection') }}</th>
          <th>{{ t('join.colBattery') }}</th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.serial">
          <td>
            <input
              v-model="selectedSerials"
              type="checkbox"
              :value="d.serial"
              :disabled="Boolean(activeRunBySerial[d.serial]) || startingSelected"
              :aria-label="t('join.selectDevice', { device: d.nickname || d.model })"
            />
          </td>
          <td>
            <input
              class="nickname-input"
              :value="d.nickname ?? ''"
              :placeholder="t('join.nicknamePlaceholder')"
              @change="onSetNickname(d.serial, $event.target.value)"
            />
          </td>
          <td>{{ d.model }}</td>
          <td>
            <StatusBadge
              :label="d.connection === 'wifi' ? 'WiFi' : 'USB'"
              :tone="d.connection === 'wifi' ? 'success' : 'neutral'"
            />
          </td>
          <td>{{ d.battery_level ?? '—' }}{{ d.battery_level != null ? '%' : '' }}</td>
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
          <td>
            <button
              v-if="!activeRunBySerial[d.serial]"
              @click="onStartTest(d.serial)"
              :disabled="startingSerial === d.serial"
            >
              {{ startingSerial === d.serial ? t('join.starting') : t('join.startTestButton') }}
            </button>
            <template v-else>
              <StatusBadge
                :label="activeRunBySerial[d.serial].status"
                :tone="STATUS_TONE[activeRunBySerial[d.serial].status] ?? 'neutral'"
              />
              <button
                @click="onCancelRun(activeRunBySerial[d.serial].id, d.serial)"
                :disabled="cancellingSerial === d.serial"
              >
                {{ cancellingSerial === d.serial ? '…' : t('join.cancelButton') }}
              </button>
            </template>
          </td>
        </tr>
      </tbody>
      </table>
    </template>
  </Card>
</template>

<style scoped>
.steps {
  padding-left: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.form-row {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
  align-items: center;
}
.form-row input {
  flex: 1;
  min-width: 200px;
}
.mdns-table {
  margin-top: var(--space-3);
}
.mdns-table code {
  white-space: nowrap;
}
.endpoint-label {
  display: inline-block;
  min-width: 5em;
  margin-right: var(--space-2);
  color: var(--color-text-muted);
  font-size: 0.8em;
}
.result-success,
.result-error {
  margin: var(--space-1) 0 0;
  font-size: 0.8em;
}
.result-success {
  color: var(--color-success);
}
.result-error {
  color: var(--color-danger);
}
.nickname-input {
  width: 100%;
  max-width: 160px;
}
.bulk-actions {
  display: flex;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
  align-items: end;
}
.scenario-picker {
  display: flex;
  flex: 1 1 360px;
  flex-direction: column;
  gap: var(--space-1);
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.scenario-picker select {
  width: 100%;
}
.suite-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
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
.info {
  color: var(--color-success);
  font-size: 0.9em;
}
</style>
