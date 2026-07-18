import { createI18n } from 'vue-i18n'
import zhTW from './locales/zh-TW.js'
import en from './locales/en.js'

const STORAGE_KEY = 'autoperf-locale'

function initialLocale() {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'zh-TW' || saved === 'en') return saved
  return navigator.language?.startsWith('zh') ? 'zh-TW' : 'en'
}

const i18n = createI18n({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'en',
  messages: { 'zh-TW': zhTW, en },
})

export function setLocale(locale) {
  i18n.global.locale.value = locale
  localStorage.setItem(STORAGE_KEY, locale)
  document.documentElement.lang = locale
}

document.documentElement.lang = i18n.global.locale.value

export default i18n
