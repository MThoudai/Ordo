import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5176,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_PYTHON_API_URL || 'http://127.0.0.1:18000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      },
      '/openapi.json': {
        target: process.env.VITE_PYTHON_API_URL || 'http://127.0.0.1:18000',
        changeOrigin: true
      }
    }
  }
})
