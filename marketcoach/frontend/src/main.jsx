import React from 'react'
import ReactDOM from 'react-dom/client'
import axios from 'axios'
import App from './App'
import './index.css'

// Install bearer auth on every axios request before React mounts. The
// backend's require_auth bypasses auth when API_SECRET is empty (dev
// default), so an empty VITE_API_SECRET is a no-op and the local dev
// flow still works unchanged. When the backend has API_SECRET set
// (production, Railway, any deploy you care about), the frontend reads
// the matching secret from VITE_API_SECRET and attaches it here so
// every component's axios call is authenticated without per-component
// plumbing.
//
// Security note: VITE_* vars are inlined into the client bundle at
// build time. This is fine for local dev because the dev server isn't
// exposed to the internet, but DO NOT ship a built frontend with a
// real API secret baked in — that's giving the secret to anyone who
// loads the page. The production frontend needs a different auth story
// (session cookies, OAuth, or a reverse proxy that injects the header).
const apiSecret = import.meta.env.VITE_API_SECRET
if (apiSecret) {
  axios.defaults.headers.common['Authorization'] = `Bearer ${apiSecret}`
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
