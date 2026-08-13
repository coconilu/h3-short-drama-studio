import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
})
