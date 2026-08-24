import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  base: '/static/',
  plugins: [react()],
  // Build into ../static/ so FastAPI can serve the production bundle
  build: {
    outDir: path.resolve(__dirname, '..', 'static'),
    emptyOutDir: true,
  },
  // Proxy API and WebSocket calls to the FastAPI backend during dev
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8765',
        ws: true,
      },
    },
  },
})
