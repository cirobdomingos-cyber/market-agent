import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // host: true binds 0.0.0.0 so other devices on the same network
    // (e.g. phone via Tailscale) can reach the dev server. The backend
    // stays bound to 127.0.0.1 — all API traffic flows through the proxy
    // below, so the backend is never directly exposed.
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
