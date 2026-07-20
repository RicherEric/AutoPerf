<script setup>
import { onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDeviceScreen } from '../composables/useDeviceScreen.js'
import StatusBadge from './StatusBadge.vue'

// Thin wrapper around useDeviceScreen() for embedding a live device screen
// next to other content (e.g. Run Detail's metric charts), rather than as
// its own page. Auto-connects/disconnects from `serial`/`active` instead of
// requiring a manual Connect button -- `active` should track whatever
// condition means "there's something worth watching right now" (e.g. the
// run is still `running`), so navigating away or the run finishing releases
// the device's single screen-capture slot instead of leaving a stream up.
const props = defineProps({
  serial: { type: String, default: '' },
  active: { type: Boolean, default: false },
})

const { t } = useI18n()
const { canvas, connectionState, errorMessage, connect, disconnect } = useDeviceScreen()

function sync() {
  if (props.active && props.serial) {
    connect(props.serial)
  } else {
    disconnect()
  }
}

watch(() => [props.serial, props.active], sync, { immediate: true })
onUnmounted(disconnect)
</script>

<template>
  <div class="live-screen-panel">
    <p v-if="errorMessage" class="error">{{ errorMessage }}</p>
    <StatusBadge
      v-if="connectionState !== 'idle'"
      :label="t(`screen.state.${connectionState}`)"
      :tone="connectionState === 'streaming' ? 'success' : connectionState === 'error' ? 'danger' : 'warning'"
    />
    <p v-else class="hint">{{ t('runDetail.liveScreenIdleHint') }}</p>
    <canvas ref="canvas" class="screen-canvas"></canvas>
  </div>
</template>

<style scoped>
.screen-canvas {
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  margin-top: var(--space-2);
  background: var(--color-surface-alt);
}
.hint {
  color: var(--color-text-muted);
  font-size: 0.85em;
}
</style>
