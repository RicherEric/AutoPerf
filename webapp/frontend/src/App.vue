<script setup>
import { useRoute } from 'vue-router'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLocale } from './i18n.js'

const route = useRoute()
const isRunSection = computed(() => route.path === '/runs' || route.path.startsWith('/runs/'))
const { t, locale } = useI18n()

function toggleLocale() {
  setLocale(locale.value === 'zh-TW' ? 'en' : 'zh-TW')
}
</script>

<template>
  <nav class="nav">
    <router-link to="/" class="brand">
      <img src="/favicon.svg" alt="" class="brand-logo" />
      <strong>AutoPerf</strong>
    </router-link>
    <router-link to="/" active-class="nav-active">{{ t('nav.stats') }}</router-link>
    <router-link to="/runs" :class="{ 'nav-active': isRunSection }">{{ t('nav.runs') }}</router-link>
    <router-link to="/queue" active-class="nav-active">{{ t('nav.queue') }}</router-link>
    <router-link to="/screen" active-class="nav-active">{{ t('nav.screen') }}</router-link>
    <router-link to="/mission-control" active-class="nav-active">{{ t('nav.missionControl') }}</router-link>
    <router-link to="/join" active-class="nav-active">{{ t('nav.join') }}</router-link>
    <button class="locale-toggle" @click="toggleLocale">
      {{ locale === 'zh-TW' ? 'EN' : '中文' }}
    </button>
  </nav>
  <main class="content">
    <router-view />
  </main>
</template>

<style scoped>
.nav .brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  text-decoration: none;
  color: var(--color-text);
  border-bottom: none;
  margin-right: var(--space-2);
}
.brand-logo {
  width: 22px;
  height: 22px;
}
.locale-toggle {
  margin-left: auto;
  font-size: 0.8rem;
  padding: 4px 10px;
}
.content {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5);
}
</style>
