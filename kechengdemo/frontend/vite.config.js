import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const API = 'http://127.0.0.1:8848'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: API, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
