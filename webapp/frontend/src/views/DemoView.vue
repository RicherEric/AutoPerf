<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  getDemoPlan,
  getRunningRuns,
  listConnectedDevices,
  listRuns,
  triggerRun,
} from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()

const plan = ref(null)
const devices = ref([])
// Several, not one. Two phones running the same sequence at the same time is
// the thing being demonstrated -- a single-choice picker cannot show it.
const selected = ref(new Set())
const error = ref('')
const notice = ref('')
const starting = ref(false)
const prewarmStep = ref(-1)
const runs = ref([])
const running = ref([])
let pollHandle = null

const chosen = computed(() => devices.value.filter((d) => selected.value.has(d.serial)))
const perDevice = computed(() => plan.value?.integration_seconds ?? 0)

function toggle(serial) {
  const next = new Set(selected.value)
  next.has(serial) ? next.delete(serial) : next.add(serial)
  selected.value = next
}

function clock(seconds) {
  const m = Math.floor(seconds / 60)
  return `${m}:${String(Math.round(seconds % 60)).padStart(2, '0')}`
}

function deviceLabel(serial) {
  const d = devices.value.find((x) => x.serial === serial)
  return d ? d.nickname || d.model : serial
}

async function refresh() {
  try {
    const [current, recent] = await Promise.all([getRunningRuns(), listRuns()])
    running.value = current.running_runs ?? []
    runs.value = recent
  } catch (err) {
    error.value = err.message
  }
}

// Every device gets the whole sequence, queued in order. They run in
// parallel because each device has its own lock -- so two phones cost the
// same wall-clock as one, which is the claim on screen.
async function startOn(steps) {
  error.value = ''
  notice.value = ''
  starting.value = true
  try {
    let queued = 0
    for (const [index, step] of steps.entries()) {
      prewarmStep.value = index
      for (const device of chosen.value) {
        await triggerRun(device.serial, step.duration, step.scenario, step.blind ?? [])
        queued += 1
      }
    }
    notice.value = t('demo.queued', { count: queued, devices: chosen.value.length })
    await refresh()
  } catch (err) {
    error.value = err.message
  } finally {
    starting.value = false
    prewarmStep.value = -1
  }
}

onMounted(async () => {
  try {
    ;[plan.value, devices.value] = await Promise.all([getDemoPlan(), listConnectedDevices()])
    selected.value = new Set(devices.value.map((d) => d.serial))   // 預設全選
  } catch (err) {
    error.value = err.message
  }
  await refresh()
  pollHandle = setInterval(refresh, 3000)
})

onUnmounted(() => clearInterval(pollHandle))
</script>

<template>
  <Card :title="t('demo.title')">
    <p class="hint">{{ t('demo.intro') }}</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>

    <p class="label">{{ t('demo.deviceLabel') }}</p>
    <div class="devices">
      <label v-for="d in devices" :key="d.serial" class="device">
        <input type="checkbox" :checked="selected.has(d.serial)" @change="toggle(d.serial)" />
        <span class="name">{{ d.nickname || d.model }}</span>
        <code>{{ d.serial }}</code>
      </label>
    </div>
    <p v-if="!devices.length" class="hint">{{ t('demo.noDevices') }}</p>
  </Card>

  <Card :title="t('demo.integrationTitle')">
    <p class="hint">{{ t('demo.integrationHint') }}</p>

    <div class="start-bar">
      <button class="start" :disabled="starting || !chosen.length"
              @click="startOn(plan.integration)">
        {{ starting ? t('demo.starting') : t('demo.startButton') }}
      </button>
      <span v-if="chosen.length" class="plan">
        {{ t('demo.plan', {
          devices: chosen.length,
          scenarios: plan?.integration?.length ?? 0,
          total: chosen.length * (plan?.integration?.length ?? 0),
          time: clock(perDevice),
        }) }}
      </span>
    </div>

    <ol v-if="plan" class="steps">
      <li v-for="(step, index) in plan.integration" :key="step.scenario"
          :class="{ current: index === prewarmStep }">
        <code>{{ step.scenario }}</code>
        <span class="secs">{{ step.duration }}s</span>
        <em>{{ step.why }}</em>
      </li>
    </ol>

    <!-- Two devices at once is the claim, so it has to be visible while it
         happens -- not inferred afterwards from the run list. -->
    <div class="now">
      <p class="label">{{ t('demo.runningNow') }}</p>
      <p v-if="!running.length" class="hint">{{ t('demo.nothingRunning') }}</p>
      <div v-else class="running">
        <div v-for="r in running" :key="r.id" class="chip">
          <strong>{{ deviceLabel(r.device_serial) }}</strong>
          <code>{{ r.youtube_scenario }}</code>
        </div>
      </div>
    </div>
  </Card>

  <Card :title="t('demo.prewarmTitle')">
    <p class="hint">{{ t('demo.prewarmHint') }}</p>
    <button :disabled="starting || !chosen.length" @click="startOn(plan.prewarm)">
      {{ t('demo.prewarmButton') }}
    </button>
  </Card>
</template>

<style scoped>
.label {
  font-size: 0.85em;
  font-weight: 600;
  margin: var(--space-3) 0 var(--space-1);
}
.devices {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.device {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 0.9em;
}
.device .name {
  font-weight: 600;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.notice {
  color: var(--color-success-text, var(--color-text));
  font-size: 0.9em;
}
.start-bar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}
.start {
  font-size: 1.05em;
  font-weight: 600;
  padding: var(--space-3) var(--space-4);
}
.plan {
  font-size: 0.9em;
  font-weight: 600;
}
.steps {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-4);
  font-size: 0.9em;
}
.steps li {
  margin-bottom: var(--space-1);
}
.steps li.current {
  font-weight: 600;
}
.steps .secs {
  color: var(--color-text-muted);
  margin: 0 var(--space-2);
  font-size: 0.85em;
}
.steps em {
  display: block;
  font-style: normal;
  color: var(--color-text-muted);
  font-size: 0.85em;
}
.now {
  border-top: 1px solid var(--color-border, #ddd);
  padding-top: var(--space-2);
}
.running {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.running .chip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: 999px;
  background: var(--color-surface-alt, rgba(0, 0, 0, 0.05));
  font-size: 0.9em;
}
</style>
