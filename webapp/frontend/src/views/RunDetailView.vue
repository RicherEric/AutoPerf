<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { getRun, listSamples } from '../api.js'

const props = defineProps({ id: String })

const run = ref(null)
const samples = ref([])
const error = ref('')
const sinceId = ref(0)

let pollHandle = null
const TERMINAL_STATUSES = ['completed', 'failed', 'interrupted']

async function poll() {
  try {
    run.value = await getRun(props.id)
    const result = await listSamples(props.id, sinceId.value)
    samples.value = [...samples.value, ...result.samples].slice(-200)
    sinceId.value = result.next_since_id
    if (run.value && TERMINAL_STATUSES.includes(run.value.status)) {
      clearInterval(pollHandle)
    }
  } catch (err) {
    error.value = err.message
    clearInterval(pollHandle)
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
  <section>
    <p><router-link to="/">&larr; Back to runs</router-link></p>
    <h2>Run {{ id }}</h2>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="run">
      <p>Status: <strong>{{ run.status }}</strong></p>
      <p>Device: {{ run.device_serial }}</p>
      <p v-if="run.error" class="error">Error: {{ run.error }}</p>
    </div>
  </section>

  <section>
    <h2>Recent metric samples</h2>
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Collector</th>
          <th>Name</th>
          <th>Value</th>
          <th>Unit</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="sample in samples.slice().reverse()" :key="sample.id">
          <td>{{ sample.timestamp }}</td>
          <td>{{ sample.collector }}</td>
          <td>{{ sample.name }}</td>
          <td>{{ sample.value }}</td>
          <td>{{ sample.unit }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
