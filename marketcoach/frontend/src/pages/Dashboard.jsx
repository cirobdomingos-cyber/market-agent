import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import axios from 'axios'
import ReactMarkdown from 'react-markdown'
import SignalCard from '../components/SignalCard'
import ThesisPanel from '../components/ThesisPanel'
import EquityChart from '../components/EquityChart'

const API = '/api'

function formatBriefTime(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function isStaleBrief(iso) {
  if (!iso) return true
  try {
    const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000
    return hours > 24
  } catch {
    return true
  }
}

export default function Dashboard() {
  const [signals, setSignals] = useState([])
  const [theses, setTheses] = useState([])
  const [accuracy, setAccuracy] = useState(null)
  const [brief, setBrief] = useState(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [generatingBrief, setGeneratingBrief] = useState(false)
  const [error, setError] = useState(null)

  const fetchData = async () => {
    try {
      const [sigRes, thRes, accRes, briefRes] = await Promise.all([
        axios.get(`${API}/signals?limit=20`),
        axios.get(`${API}/theses?open_only=true`),
        axios.get(`${API}/accuracy`),
        axios.get(`${API}/morning-brief/latest`).catch((err) => {
          if (err.response?.status === 404) return { data: null }
          throw err
        }),
      ])
      setSignals(sigRes.data)
      setTheses(thRes.data)
      setAccuracy(accRes.data)
      setBrief(briefRes.data)
    } catch (err) {
      console.error('Failed to fetch dashboard data:', err)
    } finally {
      setLoading(false)
    }
  }

  const runMorningBrief = async () => {
    setGeneratingBrief(true)
    setError(null)
    try {
      const res = await axios.post(`${API}/morning-brief/run`)
      setBrief(res.data)
    } catch (err) {
      const msg = err.response?.data?.detail || err.message || 'Brief generation failed'
      setError(msg)
    } finally {
      setGeneratingBrief(false)
    }
  }

  useEffect(() => { fetchData() }, [])

  const runPipeline = async () => {
    setRunning(true)
    setError(null)
    try {
      await axios.post(`${API}/pipeline/run`)
      await fetchData()
    } catch (err) {
      const msg = err.response?.data?.detail || err.message || 'Pipeline failed'
      setError(msg)
    } finally {
      setRunning(false)
    }
  }

  if (loading) {
    return <p className="text-gray-500 text-sm">Loading dashboard...</p>
  }

  return (
    <div className="space-y-6">
      {/* Header with pipeline trigger + accuracy summary */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-xl font-bold">Market Intelligence</h1>
          {accuracy && accuracy.total > 0 && (
            <div className="flex items-center gap-3 text-sm">
              <span className="text-gray-400">
                Accuracy: <span className="text-indigo-400 font-bold">{accuracy.accuracy_pct}%</span>
              </span>
              <span className="text-gray-600">|</span>
              <span className="text-gray-400">
                <span className="text-green-400">{accuracy.correct}</span>
                {' / '}
                <span className="text-white">{accuracy.total}</span> correct
              </span>
            </div>
          )}
        </div>
        <button
          onClick={runPipeline}
          disabled={running}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40
                     rounded text-sm font-medium transition-colors"
        >
          {running ? 'Running pipeline...' : 'Run Pipeline'}
        </button>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Morning brief — prominent card so it's the first thing you see
          on phone wake-up. Auto-refreshes with latest from the cron. */}
      <div className="bg-gradient-to-br from-indigo-950/40 to-gray-900 border border-indigo-800/40 rounded-lg p-5">
        <div className="flex items-start justify-between gap-3 mb-3">
          <div>
            <h2 className="text-sm font-semibold text-indigo-300 uppercase tracking-wider">
              Morning Brief
            </h2>
            {brief && (
              <p className="text-xs text-gray-500 mt-0.5">
                {formatBriefTime(brief.created_at)}
                {brief.trigger === 'manual' && ' · manual'}
                {isStaleBrief(brief.created_at) && (
                  <span className="ml-2 text-amber-400">· over 24h old</span>
                )}
              </p>
            )}
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              onClick={runMorningBrief}
              disabled={generatingBrief}
              className="px-3 py-1.5 text-xs bg-indigo-700 hover:bg-indigo-600 disabled:bg-gray-800 disabled:text-gray-500 text-white rounded transition"
            >
              {generatingBrief ? 'Generating…' : brief ? 'Refresh' : 'Run now'}
            </button>
            <Link
              to="/morning-brief"
              className="px-3 py-1.5 text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 rounded transition"
            >
              Archive
            </Link>
          </div>
        </div>

        {brief ? (
          brief.status === 'failed' ? (
            <div className="text-sm text-red-300">
              Generation failed: {brief.error || 'unknown error'}
            </div>
          ) : (
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown>{brief.content}</ReactMarkdown>
            </div>
          )
        ) : (
          <p className="text-sm text-gray-400">
            No morning brief yet. The next scheduled run is weekdays at 06:00,
            or click <strong>Run now</strong> to generate one immediately.
          </p>
        )}
      </div>

      {/* Equity chart — shows how the account has progressed over time.
          Backed by the equity_snapshots table, populated every 5 min by
          the position polling job. */}
      <EquityChart />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Signal Feed */}
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
            Signal Feed ({signals.length})
          </h2>
          <div className="space-y-2 max-h-[70vh] overflow-y-auto pr-1">
            {signals.length === 0 ? (
              <p className="text-gray-500 text-sm">No signals yet. Run the pipeline.</p>
            ) : (
              signals.map((s) => <SignalCard key={s.id} signal={s} />)
            )}
          </div>
        </div>

        {/* Active Theses */}
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
            Active Theses ({theses.length})
          </h2>
          <div className="space-y-2 max-h-[70vh] overflow-y-auto pr-1">
            {theses.length === 0 ? (
              <p className="text-gray-500 text-sm">No open theses yet.</p>
            ) : (
              theses.map((t) => <ThesisPanel key={t.id} thesis={t} />)
            )}
          </div>
        </div>

        {/* Resolved theses (accuracy detail) */}
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
            Track Record
          </h2>
          {accuracy && accuracy.total > 0 ? (
            <div className="space-y-3">
              <div className="border border-gray-800 rounded-lg p-4 space-y-3">
                <div className="text-center">
                  <span className="text-4xl font-bold text-indigo-400">
                    {accuracy.accuracy_pct}%
                  </span>
                  <p className="text-gray-400 text-xs mt-1">Prediction Accuracy</p>
                </div>
                <div className="grid grid-cols-3 gap-2 text-center text-sm">
                  <div>
                    <p className="text-white font-medium">{accuracy.total}</p>
                    <p className="text-gray-500 text-xs">Total</p>
                  </div>
                  <div>
                    <p className="text-green-400 font-medium">{accuracy.correct}</p>
                    <p className="text-gray-500 text-xs">Correct</p>
                  </div>
                  <div>
                    <p className="text-red-400 font-medium">{accuracy.incorrect}</p>
                    <p className="text-gray-500 text-xs">Wrong</p>
                  </div>
                </div>
                {/* Simple visual bar */}
                <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-green-500 to-indigo-500 rounded-full"
                    style={{ width: `${accuracy.accuracy_pct}%` }}
                  />
                </div>
              </div>
            </div>
          ) : (
            <p className="text-gray-500 text-sm">
              Theses will be auto-resolved when they expire. Run the pipeline to generate theses.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
