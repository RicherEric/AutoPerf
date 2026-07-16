<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { getQueueStatus } from '../api.js'
import Card from '../components/Card.vue'
import StatusBadge from '../components/StatusBadge.vue'

const status = ref(null)
const error = ref('')
let pollHandle = null

async function poll() {
  try {
    status.value = await getQueueStatus()
  } catch (err) {
    error.value = err.message
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
  <Card title="Task queue">
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="status">
      <p v-if="!status.broker_reachable">
        <StatusBadge label="Broker unreachable" tone="danger" />
        <span v-if="status.error"> — {{ status.error }}</span>
      </p>
      <p v-else-if="!status.worker_online">
        <StatusBadge label="No worker connected" tone="warning" />
      </p>
      <p v-else>
        <StatusBadge :label="`${status.workers.length} worker(s) online`" tone="success" />
      </p>
    </div>
  </Card>

  <Card v-if="status && status.broker_reachable && !status.worker_online" title="Start a worker">
    <p>No Celery worker is currently listening on the broker. Start one:</p>
    <pre>celery -A config worker --pool=solo -l info</pre>
    <p>(run from the <code>webapp</code> directory, with Redis running)</p>
  </Card>

  <Card v-if="status?.running_runs?.length" title="Currently running">
    <p class="hint">
      Sourced from Storage, not Celery -- <code>--pool=solo</code> is fully synchronous, so
      the worker can't answer an inspect() request while it's busy executing a task
      (the section above may show 0 active tasks even though a run is genuinely in
      progress). This list stays accurate regardless.
    </p>
    <table>
      <thead>
        <tr>
          <th>Run</th>
          <th>Device</th>
          <th>Started</th>
          <th>Checkpoint (s)</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in status.running_runs" :key="run.id">
          <td><router-link :to="`/runs/${run.id}`">{{ run.id.slice(0, 8) }}</router-link></td>
          <td>{{ run.device_serial }}</td>
          <td>{{ run.started_at ?? '—' }}</td>
          <td>{{ run.checkpoint ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
  </Card>

  <Card v-for="worker in status?.workers ?? []" :key="worker.name" :title="worker.name">
    <div class="stat-row">
      <div class="stat-tile">
        <span class="value">{{ worker.active.length }}</span>
        <span class="label">Active</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ worker.reserved.length }}</span>
        <span class="label">Reserved</span>
      </div>
      <div class="stat-tile">
        <span class="value">{{ worker.scheduled.length }}</span>
        <span class="label">Scheduled</span>
      </div>
    </div>
    <table v-if="worker.active.length || worker.reserved.length">
      <thead>
        <tr>
          <th>Task ID</th>
          <th>Name</th>
          <th>State</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="task in worker.active" :key="task.id">
          <td>{{ task.id.slice(0, 12) }}</td>
          <td>{{ task.name }}</td>
          <td>active</td>
        </tr>
        <tr v-for="task in worker.reserved" :key="task.id">
          <td>{{ task.id.slice(0, 12) }}</td>
          <td>{{ task.name }}</td>
          <td>reserved</td>
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
