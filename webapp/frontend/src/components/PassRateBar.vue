<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  passRate: { type: Number, default: null }, // 0..1 or null when nothing to evaluate yet
  passCount: { type: Number, default: 0 },
  failCount: { type: Number, default: 0 },
})

const widthPct = computed(() => (props.passRate === null ? 0 : Math.round(props.passRate * 100)))

const tone = computed(() => {
  if (props.passRate === null) return 'neutral'
  if (props.passRate >= 0.8) return 'ok'
  if (props.passRate >= 0.5) return 'warn'
  return 'danger'
})
</script>

<template>
  <div class="pass-rate-row">
    <span class="pass-rate-label">{{ label }}</span>
    <div class="pass-rate-track">
      <div class="pass-rate-fill" :class="`pass-rate-${tone}`" :style="{ width: widthPct + '%' }" />
    </div>
    <span class="pass-rate-value">
      {{ passRate === null ? '—' : `${widthPct}%` }}
      <span class="pass-rate-counts">({{ passCount }}/{{ passCount + failCount }})</span>
    </span>
  </div>
</template>

<style scoped>
.pass-rate-row {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) 2fr auto;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-1) 0;
}
.pass-rate-label {
  font-size: 0.9em;
}
.pass-rate-track {
  height: 10px;
  background: var(--color-surface-alt);
  border-radius: 999px;
  overflow: hidden;
}
.pass-rate-fill {
  height: 100%;
  border-radius: 999px;
}
.pass-rate-ok {
  background: var(--color-success-bg);
}
.pass-rate-warn {
  background: var(--color-warning-bg);
}
.pass-rate-danger {
  background: var(--color-danger-bg);
}
.pass-rate-neutral {
  background: var(--color-neutral-bg);
}
.pass-rate-value {
  font-size: 0.85em;
  font-weight: 600;
  white-space: nowrap;
}
.pass-rate-counts {
  color: var(--color-text-muted);
  font-weight: 400;
}
</style>
