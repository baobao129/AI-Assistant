import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：启用 Vue 插件，并把 /api 代理到后端（默认 127.0.0.1:8000，可用 BACKEND_URL 覆盖）。
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: { '/api': { target: process.env.BACKEND_URL || 'http://127.0.0.1:8000' } },
  },
})
