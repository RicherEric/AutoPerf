<script setup>
import { useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const isRunSection = computed(() => route.path === '/runs' || route.path.startsWith('/runs/'))
</script>

<template>
  <nav class="nav">
    <strong>AutoPerf</strong>
    <router-link to="/" active-class="nav-active">統計概覽</router-link>
    <router-link to="/runs" :class="{ 'nav-active': isRunSection }">Run List</router-link>
    <router-link to="/queue" active-class="nav-active">Task Queue</router-link>
    <router-link to="/screen" active-class="nav-active">Device Screen</router-link>
  </nav>
  <main class="content">
    <router-view />
  </main>
</template>

<style scoped>
.content {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5);
}
</style>
