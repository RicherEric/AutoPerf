<script setup>
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'

// A lightweight "how's this phone doing" indicator for the device list.
// Only `battery_level` is available here (a static per-device registry
// field, GET /api/devices) -- CPU% and battery temperature are per-run
// metric samples, not device properties, so they can't drive this badge
// outside of an active run.
const props = defineProps({
  device: { type: Object, required: true },
})

const mood = computed(() => {
  const level = props.device.battery_level
  if (level == null) return { emoji: '❓', tone: 'neutral' }
  if (level <= 15) return { emoji: '🪫', tone: 'danger' }
  if (level <= 40) return { emoji: '😐', tone: 'warning' }
  return { emoji: '😀', tone: 'success' }
})
</script>

<template>
  <StatusBadge :label="mood.emoji" :tone="mood.tone" />
</template>
