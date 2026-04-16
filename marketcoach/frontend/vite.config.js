import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Resolves the backend URL at dev-server start. Reads VITE_API_URL from
// .env / .env.local / .env.<mode> in this directory. Empty or unset falls
// back to the local backend on port 8000 (original behaviour).
//
// Typical values:
//   VITE_API_URL=http://localhost:8000              ← local backend (default)
//   VITE_API_URL=https://<your>.up.railway.app      ← cloud backend (Phase 1)
//
// Both the target URL and the bearer secret live in frontend/.env.local
// (see frontend/.env.example). .env.local is gitignored at the repo root.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_API_URL || 'http://localhost:8000'

  return {
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
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
          // When the target is HTTPS (Railway), secure:true verifies certs.
          // Keep it strict — a MITM between Vite and Railway would be a
          // real problem and we don't want a silent downgrade.
          secure: true,
        },
      },
    },
  }
})
