<script setup>
defineProps({
  message: { type: String, required: true },
  tone: { type: String, default: 'danger' }, // success | warning | danger | neutral
})
</script>

<template>
  <div class="toast" :class="`toast-${tone}`">{{ message }}</div>
</template>

<style scoped>
.toast {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-2);
  font-size: 0.9em;
}
.toast-success {
  background: var(--color-success-bg);
  color: var(--color-success-text);
}
.toast-warning {
  background: var(--color-warning-bg);
  color: var(--color-warning-text);
}
.toast-danger {
  background: var(--color-danger-bg);
  color: var(--color-danger-text);
}
.toast-neutral {
  background: var(--color-neutral-bg);
  color: var(--color-neutral-text);
}
</style>
