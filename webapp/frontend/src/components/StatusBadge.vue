<script setup>
defineProps({
  label: { type: String, required: true },
  tone: { type: String, default: 'neutral' }, // success | warning | danger | neutral
})
</script>

<template>
  <span class="badge" :class="`badge-${tone}`">{{ label }}</span>
</template>
