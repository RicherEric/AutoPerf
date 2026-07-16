<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  name: { type: String, required: true },
  unit: { type: String, default: '' },
  samples: { type: Array, default: () => [] }, // [{ timestamp, value }] for this metric only
  xDomain: { type: Array, default: null }, // [minEpochMs, maxEpochMs], shared across the chart grid
  baselineMean: { type: Number, default: null },
  regressed: { type: Boolean, default: null },
})

// Fixed order (never reassigned/cycled) matching the validated categorical
// palette in theme.css -- see that file's header comment for how these four
// hex values were chosen and validated.
const SERIES_COLOR_BY_METRIC = {
  'cpu.total': 'var(--chart-series-1)',
  'memory.used': 'var(--chart-series-2)',
  'battery.level': 'var(--chart-series-3)',
  'battery.temperature': 'var(--chart-series-4)',
}
const DEFAULT_SERIES_COLOR = 'var(--chart-series-1)'

const WIDTH = 340
const HEIGHT = 120
const PAD_LEFT = 44
const PAD_RIGHT = 12
const PAD_TOP = 10
const PAD_BOTTOM = 10
const plotWidth = WIDTH - PAD_LEFT - PAD_RIGHT
const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM

const hoverIndex = ref(null)

const parsed = computed(() =>
  props.samples.map((s) => ({ t: new Date(s.timestamp).getTime(), value: s.value }))
)

const xDomain = computed(() => {
  if (props.xDomain) return props.xDomain
  const times = parsed.value.map((p) => p.t)
  return times.length ? [Math.min(...times), Math.max(...times)] : [0, 1]
})

const yDomain = computed(() => {
  const values = parsed.value.map((p) => p.value)
  if (!values.length) return [0, 1]
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (props.baselineMean !== null) {
    min = Math.min(min, props.baselineMean)
    max = Math.max(max, props.baselineMean)
  }
  if (min === max) {
    min -= 1
    max += 1
  }
  const pad = (max - min) * 0.1
  return [min - pad, max + pad]
})

function toX(t) {
  const [x0, x1] = xDomain.value
  if (x1 === x0) return PAD_LEFT
  return PAD_LEFT + ((t - x0) / (x1 - x0)) * plotWidth
}

function toY(value) {
  const [y0, y1] = yDomain.value
  if (y1 === y0) return PAD_TOP + plotHeight / 2
  return PAD_TOP + plotHeight - ((value - y0) / (y1 - y0)) * plotHeight
}

const points = computed(() => parsed.value.map((p) => ({ ...p, x: toX(p.t), y: toY(p.value) })))

const pathD = computed(() => {
  const pts = points.value
  if (!pts.length) return ''
  return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
})

const lastPoint = computed(() => points.value[points.value.length - 1] ?? null)
const baselineY = computed(() => (props.baselineMean === null ? null : toY(props.baselineMean)))

function formatValue(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 })
}

function onMove(event) {
  const pts = points.value
  if (!pts.length) return
  const rect = event.currentTarget.getBoundingClientRect()
  const px = ((event.clientX - rect.left) / rect.width) * WIDTH
  let closest = 0
  let closestDist = Infinity
  pts.forEach((p, i) => {
    const dist = Math.abs(p.x - px)
    if (dist < closestDist) {
      closestDist = dist
      closest = i
    }
  })
  hoverIndex.value = closest
}

function onLeave() {
  hoverIndex.value = null
}

const hoverPoint = computed(() => (hoverIndex.value === null ? null : points.value[hoverIndex.value]))
const seriesColor = computed(() => SERIES_COLOR_BY_METRIC[props.name] ?? DEFAULT_SERIES_COLOR)
</script>

<template>
  <div class="metric-chart">
    <div class="metric-chart-title">{{ name }} <span class="unit">({{ unit }})</span></div>
    <svg :viewBox="`0 0 ${WIDTH} ${HEIGHT}`" preserveAspectRatio="none">
      <line
        :x1="PAD_LEFT" :y1="toY(yDomain[0])" :x2="WIDTH - PAD_RIGHT" :y2="toY(yDomain[0])"
        class="axis-line"
      />
      <text :x="PAD_LEFT - 6" :y="toY(yDomain[1]) + 4" class="axis-label" text-anchor="end">
        {{ formatValue(yDomain[1]) }}
      </text>
      <text :x="PAD_LEFT - 6" :y="toY(yDomain[0])" class="axis-label" text-anchor="end">
        {{ formatValue(yDomain[0]) }}
      </text>

      <line
        v-if="baselineY !== null"
        :x1="PAD_LEFT" :y1="baselineY" :x2="WIDTH - PAD_RIGHT" :y2="baselineY"
        class="baseline-line"
        :class="regressed ? 'baseline-danger' : 'baseline-ok'"
      />

      <path :d="pathD" class="series-line" :style="{ stroke: seriesColor }" fill="none" />

      <g v-if="lastPoint">
        <circle :cx="lastPoint.x" :cy="lastPoint.y" r="5" class="end-marker-ring" />
        <circle :cx="lastPoint.x" :cy="lastPoint.y" r="4" :style="{ fill: seriesColor }" />
        <text :x="lastPoint.x" :y="lastPoint.y - 8" class="end-label" text-anchor="end">
          {{ formatValue(lastPoint.value) }}
        </text>
      </g>

      <g v-if="hoverPoint">
        <line :x1="hoverPoint.x" :y1="PAD_TOP" :x2="hoverPoint.x" :y2="HEIGHT - PAD_BOTTOM" class="crosshair" />
        <circle :cx="hoverPoint.x" :cy="hoverPoint.y" r="4" class="hover-dot" />
        <text :x="hoverPoint.x" :y="PAD_TOP + 8" class="hover-label" :text-anchor="hoverPoint.x > WIDTH / 2 ? 'end' : 'start'">
          {{ formatValue(hoverPoint.value) }}
        </text>
      </g>

      <rect
        :x="PAD_LEFT" :y="PAD_TOP" :width="plotWidth" :height="plotHeight"
        fill="transparent" @mousemove="onMove" @mouseleave="onLeave"
      />
    </svg>
  </div>
</template>

<style scoped>
.metric-chart {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
}
.metric-chart-title {
  font-size: 0.85rem;
  font-weight: 600;
  margin-bottom: var(--space-1);
}
.metric-chart-title .unit {
  color: var(--color-text-muted);
  font-weight: 400;
}
svg {
  width: 100%;
  height: auto;
  display: block;
}
.axis-line {
  stroke: var(--chart-grid);
  stroke-width: 1;
}
.axis-label {
  fill: var(--chart-axis-text);
  font-size: 9px;
}
.series-line {
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.end-marker-ring {
  fill: var(--color-surface);
}
.end-label {
  fill: var(--color-text);
  font-size: 10px;
  font-weight: 600;
}
.baseline-line {
  stroke-width: 1.5;
  stroke-dasharray: 4 3;
}
.baseline-ok {
  stroke: var(--color-success-bg);
}
.baseline-danger {
  stroke: var(--color-danger-bg);
}
.crosshair {
  stroke: var(--color-border);
  stroke-width: 1;
}
.hover-dot {
  fill: var(--color-text);
}
.hover-label {
  fill: var(--color-text);
  font-size: 10px;
  font-weight: 600;
}
</style>
