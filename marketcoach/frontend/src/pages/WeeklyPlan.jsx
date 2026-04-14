import { useState, useEffect } from 'react'
import axios from 'axios'
import ReactMarkdown from 'react-markdown'

const API = '/api'

function formatTimestamp(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function WeeklyPlan() {
  const [plan, setPlan] = useState(null)
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)

  const fetchLatest = async () => {
    setError(null)
    try {
      const [latestRes, histRes] = await Promise.all([
        axios.get(`${API}/weekly-plan/latest`).catch((err) => {
          if (err.response?.status === 404) return { data: null }
          throw err
        }),
        axios.get(`${API}/weekly-plan?limit=10`),
      ])
      setPlan(latestRes.data)
      setHistory(histRes.data || [])
    } catch (err) {
      console.error('Failed to fetch weekly plan:', err)
      setError('Failed to load weekly plan.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchLatest()
  }, [])

  const runNow = async () => {
    setRunning(true)
    setError(null)
    try {
      const res = await axios.post(`${API}/weekly-plan/run`)
      setPlan(res.data)
      // Refresh history so the new run shows up
      const histRes = await axios.get(`${API}/weekly-plan?limit=10`)
      setHistory(histRes.data || [])
    } catch (err) {
      console.error('Failed to run weekly plan:', err)
      setError(err.response?.data?.detail || 'Failed to generate weekly plan.')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Weekly Plan</h1>
          <p className="text-sm text-gray-400 mt-1">
            Generated automatically every Sunday night. Runs Layer 1–2 macro,
            this week's key events, and specific trades to watch.
          </p>
        </div>
        <button
          onClick={runNow}
          disabled={running}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-400 text-white rounded font-medium transition"
        >
          {running ? 'Generating…' : 'Run now'}
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-800 text-red-200 rounded">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400">Loading…</div>
      ) : plan ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="flex items-center justify-between mb-4 text-xs text-gray-500 border-b border-gray-800 pb-3">
            <span>
              Generated {formatTimestamp(plan.created_at)} ({plan.trigger})
            </span>
            <span
              className={
                plan.status === 'completed'
                  ? 'text-emerald-400'
                  : 'text-red-400'
              }
            >
              {plan.status}
            </span>
          </div>
          <div className="prose prose-invert prose-sm max-w-none">
            <ReactMarkdown>{plan.content}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-center text-gray-400">
          <p>No weekly plan yet.</p>
          <p className="text-sm mt-2">
            The next scheduled run is Sunday evening, or click{' '}
            <strong>Run now</strong> to generate one immediately.
          </p>
        </div>
      )}

      {history.length > 1 && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold text-white mb-3">
            Previous plans
          </h2>
          <div className="space-y-2">
            {history.slice(1).map((p) => (
              <div
                key={p.id}
                className="bg-gray-900 border border-gray-800 rounded p-3 text-sm text-gray-400 flex justify-between"
              >
                <span>{formatTimestamp(p.created_at)}</span>
                <span className="text-xs text-gray-500">{p.trigger}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
