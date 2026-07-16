<script setup>
import { computed } from 'vue'

const props = defineProps({
  deltaPct: { type: Number, default: null },
  regressed: { type: Boolean, default: false },
})

// Capped at 100% width so one extreme outlier doesn't flatten every other bar.
const widthPct = computed(() => {
  if (props.deltaPct === null) return 0
  return Math.min(Math.abs(props.deltaPct), 100)
})
</script>

<template>
  <div class="delta-bar-track">
    <div
      class="delta-bar-fill"
      :class="regressed ? 'delta-bar-danger' : 'delta-bar-ok'"
      :style="{ width: widthPct + '%' }"
    />
  </div>
</template>

<style scoped>
.delta-bar-track {
  width: 100%;
  min-width: 80px;
  height: 8px;
  background: var(--color-surface-alt);
  border-radius: 999px;
  overflow: hidden;
}
.delta-bar-fill {
  height: 100%;
  border-radius: 999px;
}
.delta-bar-ok {
  background: var(--color-success-bg);
}
.delta-bar-danger {
  background: var(--color-danger-bg);
}
</style>
