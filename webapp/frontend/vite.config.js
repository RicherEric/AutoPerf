import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // Without an explicit host, Vite's default binding on this Windows
    // environment ended up reachable via "localhost" but NOT "127.0.0.1"
    // (an IPv4/IPv6 loopback resolution mismatch) -- every request to
    // 127.0.0.1:5173 silently failed to connect at all, which looked like a
    // blank/broken page (Task Queue and every other view) depending on
    // which address the browser tab happened to be pointed at. Binding
    // explicitly to 127.0.0.1 makes this deterministic and matches the
    // proxy target below.
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
