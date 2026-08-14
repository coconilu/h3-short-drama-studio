import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiTarget = process.env.JINGCHANG_API_URL || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': apiTarget,
    },
  },
  server: {
    proxy: {
      '/api': apiTarget,
    },
  },
})
