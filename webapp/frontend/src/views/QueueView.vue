<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { cancelRun, getQueueStatus } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const { t } = useI18n()
const status = ref(null)
const error = ref('')
const cancellingId = ref('')
let pollHandle = null

function formatTimestamp(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { hour12: false })
}

async function poll() {
  try {
    status.value = await getQueueStatus()
  } catch (err) {
    error.value = err.message
  }
}

async function onCancelRun(runId) {
  error.value = ''
  cancellingId.value = runId
  try {
    await cancelRun(runId)
    await poll()
  } catch (err) {
    error.value = err.message
  } finally {
    cancellingId.value = ''
  }
}

onMounted(async () => {
  await poll()
  pollHandle = setInterval(poll, 3000)
})

onUnmounted(() => {
  clearInterval(pollHandle)
})
</script>

<template>
  <Card :title="t('queue.title')">
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="status">
      <p v-if="!status.broker_reachable">
        <StatusBadge :label="t('queue.brokerUnreachable')" tone="danger" />
        <span v-if="status.error"> — {{ status.error }}</span>
      </p>
      <p v-else-if="!status.worker_online">
        <StatusBadge :label="t('queue.noWorker')" tone="warning" />
      </p>
      <p v-else>
        <StatusBadge :label="t('queue.workersOnline', { count: status.workers.length })" tone="success" />
      </p>
    </div>
  </Card>

  <Card v-if="status && status.broker_reachable && !status.worker_online" :title="t('queue.startWorkerTitle')">
    <p>{{ t('queue.startWorkerHint') }}</p>
    <pre>celery -A config worker --pool=solo -l info</pre>
    <p>{{ t('queue.runFromHint') }}</p>
  </Card>

  <Card v-if="status?.running_runs?.length" :title="t('queue.currentlyRunningTitle')">
    <p class="hint">{{ t('queue.sourcedHint') }}</p>
    <table>
      <thead>
        <tr>
          <th>{{ t('queue.colRun') }}</th>
          <th>{{ t('queue.colDevice') }}</th>
          <th>{{ t('queue.colStarted') }}</th>
          <th>{{ t('queue.colCheckpoint') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in status.running_runs" :key="run.id">
          <td><router-link :to="`/runs/${run.id}`">{{ run.id.slice(0, 8) }}</router-link></td>
          <td>{{ run.device_serial }}</td>
          <td>{{ formatTimestamp(run.started_at) }}</td>
          <td>{{ run.checkpoint ?? '—' }}</td>
          <td>
            <button
              v-if="!run.cancel_requested"
              @click="onCancelRun(run.id)"
              :disabled="cancellingId === run.id"
            >
              {{ cancellingId === run.id ? '…' : t('common.cancel') }}
            </button>
            <StatusBadge v-else :label="t('common.cancelling')" tone="warning" />
          </td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card v-for="worker in status?.workers ?? []" :key="worker.name" :title="worker.name">
    <div class="stat-row">
      <div class="stat-tile">
        <span class="value">{{ worker.active.length }}</span>
        <span class="label">{{ t('queue.active') }}</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ worker.reserved.length }}</span>
        <span class="label">{{ t('queue.reserved') }}</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ worker.scheduled.length }}</span>
        <span class="label">{{ t('queue.scheduled') }}</span>
      </div>
    </div>
    <table v-if="worker.active.length || worker.reserved.length">
      <thead>
        <tr>
          <th>{{ t('queue.colTaskId') }}</th>
          <th>{{ t('queue.colName') }}</th>
          <th>{{ t('queue.colState') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="task in worker.active" :key="task.id">
          <td>{{ task.id.slice(0, 12) }}</td>
          <td>{{ task.name }}</td>
          <td>{{ t('queue.active') }}</td>
        </tr>
        <tr v-for="task in worker.reserved" :key="task.id">
          <td>{{ task.id.slice(0, 12) }}</td>
          <td>{{ task.name }}</td>
          <td>{{ t('queue.reserved') }}</td>
        </tr>
      </tbody>
    </table>
  </Card>
</template>

<style scoped>
.stat-row {
  display: flex;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
.stat-row .stat-tile {
  flex: 1;
}
pre {
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-sm);
  overflow-x: auto;
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
</style>
