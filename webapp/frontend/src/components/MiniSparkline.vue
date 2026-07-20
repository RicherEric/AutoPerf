<script setup>
import { computed } from 'vue'

// A stripped-down MetricChart.vue for grids of many devices at once (e.g.
// Mission Control) -- same toX/toY scaling idea, but no axes/hover/baseline,
// just a line + an end-point dot, so many of these can render cheaply side
// by side.
const props = defineProps({
  samples: { type: Array, default: () => [] }, // [{ timestamp, value }]
})

const WIDTH = 160
const HEIGHT = 50
const PAD = 4
const plotWidth = WIDTH - PAD * 2
const plotHeight = HEIGHT - PAD * 2

const parsed = computed(() => props.samples.map((s) => ({ t: new Date(s.timestamp).getTime(), value: s.value })))

const xDomain = computed(() => {
  const times = parsed.value.map((p) => p.t)
  return times.length ? [Math.min(...times), Math.max(...times)] : [0, 1]
})

const yDomain = computed(() => {
  const values = parsed.value.map((p) => p.value)
  if (!values.length) return [0, 1]
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (min === max) {
    min -= 1
    max += 1
  }
  return [min, max]
})

function toX(t) {
  const [x0, x1] = xDomain.value
  if (x1 === x0) return PAD
  return PAD + ((t - x0) / (x1 - x0)) * plotWidth
}

function toY(value) {
  const [y0, y1] = yDomain.value
  if (y1 === y0) return PAD + plotHeight / 2
  return PAD + plotHeight - ((value - y0) / (y1 - y0)) * plotHeight
}

const points = computed(() => parsed.value.map((p) => ({ ...p, x: toX(p.t), y: toY(p.value) })))

const pathD = computed(() => {
  const pts = points.value
  if (!pts.length) return ''
  return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
})

const lastPoint = computed(() => points.value[points.value.length - 1] ?? null)
</script>

<template>
  <svg class="mini-sparkline" :viewBox="`0 0 ${WIDTH} ${HEIGHT}`" preserveAspectRatio="none">
    <path :d="pathD" fill="none" class="series-line" />
    <circle v-if="lastPoint" :cx="lastPoint.x" :cy="lastPoint.y" r="2.5" class="end-marker" />
  </svg>
</template>

<style scoped>
.mini-sparkline {
  width: 100%;
  height: 40px;
  display: block;
}
.series-line {
  stroke: var(--chart-series-1);
  stroke-width: 1.5;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.end-marker {
  fill: var(--chart-series-1);
}
</style>
