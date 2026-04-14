import { useState, useEffect } from 'react'
import axios from 'axios'

const API = '/api'

const TABS = [
  { id: 'action', label: 'Action needed' },
  { id: 'open', label: 'Open' },
  { id: 'closed', label: 'Closed' },
  { id: 'all', label: 'All' },
]

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtPct(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

function fmtDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  } catch {
    return iso
  }
}

function ThesisForm({ entryId, onSaved }) {
  const [thesis, setThesis] = useState('')
  const [disagreement, setDisagreement] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const submit = async () => {
    if (thesis.trim().length < 10) {
      setError('Thesis must be at least 10 characters')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await axios.post(`${API}/journal/${entryId}/thesis`, {
        user_thesis: thesis.trim(),
        user_disagreement: disagreement.trim() || null,
      })
      onSaved()
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to save thesis')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mt-3 p-3 bg-amber-950/20 border border-amber-900/50 rounded">
      <p className="text-xs font-semibold text-amber-200 mb-1">
        Add your thesis (manual trade detected)
      </p>
      <p className="text-[10px] text-gray-500 mb-2">
        This trade was placed directly in your broker dashboard. Add your reasoning
        now so the journal can track the calibration when it closes.
      </p>
      <textarea
        value={thesis}
        onChange={(e) => setThesis(e.target.value)}
        placeholder="In your own words, why did you take this trade?"
        rows={2}
        className="w-full bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500"
      />
      <textarea
        value={disagreement}
        onChange={(e) => setDisagreement(e.target.value)}
        placeholder="Disagreement with the advisor (optional)"
        rows={2}
        className="w-full mt-2 bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500"
      />
      {error && <p className="text-[11px] text-red-300 mt-1">{error}</p>}
      <button
        onClick={submit}
        disabled={submitting}
        className="mt-2 px-3 py-1.5 text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded"
      >
        {submitting ? 'Saving…' : 'Save thesis'}
      </button>
    </div>
  )
}

function LessonForm({ entryId, onSaved }) {
  const [lesson, setLesson] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const submit = async () => {
    if (lesson.trim().length < 10) {
      setError('Lesson must be at least 10 characters')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await axios.post(`${API}/journal/${entryId}/lesson`, {
        user_lesson: lesson.trim(),
      })
      onSaved()
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to save lesson')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mt-3 p-3 bg-emerald-950/20 border border-emerald-900/50 rounded">
      <p className="text-xs font-semibold text-emerald-200 mb-1">
        What did you learn?
      </p>
      <p className="text-[10px] text-gray-500 mb-2">
        The trade is closed. Was the advisor right or wrong? Were you? What would
        you do differently? This is the only place you build calibration.
      </p>
      <textarea
        value={lesson}
        onChange={(e) => setLesson(e.target.value)}
        placeholder="In your own words, what's the takeaway from this trade?"
        rows={3}
        className="w-full bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-emerald-600"
      />
      {error && <p className="text-[11px] text-red-300 mt-1">{error}</p>}
      <button
        onClick={submit}
        disabled={submitting}
        className="mt-2 px-3 py-1.5 text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded"
      >
        {submitting ? 'Saving…' : 'Save lesson'}
      </button>
    </div>
  )
}

function EntryCard({ entry, onChanged }) {
  const [expanded, setExpanded] = useState(entry.needs_action)
  const isClosed = entry.status === 'closed'
  const pnlPositive = (entry.pnl_pct ?? 0) >= 0

  return (
    <div
      className={`rounded-lg border p-4 ${
        entry.needs_action
          ? 'border-amber-700/60 bg-amber-950/10'
          : 'border-gray-800 bg-gray-900'
      }`}
    >
      <div
        onClick={() => setExpanded((e) => !e)}
        className="cursor-pointer flex items-center justify-between gap-3"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className={`text-xs font-bold px-2 py-0.5 rounded text-white ${
              entry.side === 'buy' ? 'bg-emerald-700' : 'bg-red-700'
            }`}
          >
            {entry.side.toUpperCase()}
          </span>
          <span className="text-white font-bold">{entry.ticker}</span>
          <span className="text-sm text-gray-300">
            {entry.qty} @ {fmtMoney(entry.open_price)}
          </span>
          <span
            className={`text-xs px-2 py-0.5 rounded border ${
              isClosed
                ? 'bg-gray-800 border-gray-700 text-gray-300'
                : 'bg-blue-900/40 border-blue-700 text-blue-200'
            }`}
          >
            {entry.status.toUpperCase()}
          </span>
          {entry.needs_action && (
            <span className="text-xs px-2 py-0.5 rounded bg-amber-700 text-white font-bold">
              ACTION NEEDED
            </span>
          )}
        </div>
        <div className="text-xs text-gray-500 shrink-0">
          {isClosed && entry.pnl_pct !== null && (
            <span
              className={`font-mono mr-2 ${
                pnlPositive ? 'text-emerald-400' : 'text-red-400'
              }`}
            >
              {fmtPct(entry.pnl_pct)}
            </span>
          )}
          {fmtDate(entry.opened_at)}
        </div>
      </div>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-800 space-y-3 text-sm">
          {/* Trade lifecycle facts */}
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <div className="flex justify-between">
              <span className="text-gray-500">Opened</span>
              <span className="text-gray-300">{fmtDate(entry.opened_at)}</span>
            </div>
            {isClosed && (
              <>
                <div className="flex justify-between">
                  <span className="text-gray-500">Closed</span>
                  <span className="text-gray-300">{fmtDate(entry.closed_at)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Exit price</span>
                  <span className="text-white font-mono">{fmtMoney(entry.close_price)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">P&amp;L</span>
                  <span
                    className={`font-mono ${
                      pnlPositive ? 'text-emerald-400' : 'text-red-400'
                    }`}
                  >
                    {fmtMoney(entry.pnl_amount)} ({fmtPct(entry.pnl_pct)})
                  </span>
                </div>
                {entry.days_held !== null && (
                  <div className="flex justify-between">
                    <span className="text-gray-500">Days held</span>
                    <span className="text-gray-300">{entry.days_held}</span>
                  </div>
                )}
                {entry.advised_direction_profitable !== null && (
                  <div className="flex justify-between">
                    <span className="text-gray-500">Direction was</span>
                    <span
                      className={
                        entry.advised_direction_profitable
                          ? 'text-emerald-400'
                          : 'text-red-400'
                      }
                    >
                      {entry.advised_direction_profitable ? 'right' : 'wrong'}
                    </span>
                  </div>
                )}
              </>
            )}
          </div>

          {/* Advisor's rationale */}
          {entry.advisor_rationale && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                Advisor said
              </p>
              <p className="text-xs text-gray-400 italic border-l-2 border-gray-700 pl-2">
                "{entry.advisor_rationale}"
              </p>
            </div>
          )}

          {/* User's thesis */}
          {entry.user_thesis ? (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-indigo-400 mb-1">
                Your thesis
              </p>
              <p className="text-xs text-gray-200 border-l-2 border-indigo-700 pl-2">
                {entry.user_thesis}
              </p>
              {entry.user_disagreement && (
                <>
                  <p className="text-[10px] uppercase tracking-wide text-indigo-400 mt-2 mb-1">
                    Where you disagreed
                  </p>
                  <p className="text-xs text-gray-200 border-l-2 border-indigo-700 pl-2">
                    {entry.user_disagreement}
                  </p>
                </>
              )}
            </div>
          ) : (
            <ThesisForm entryId={entry.id} onSaved={onChanged} />
          )}

          {/* User's lesson — only when closed */}
          {isClosed &&
            (entry.user_lesson ? (
              <div>
                <p className="text-[10px] uppercase tracking-wide text-emerald-400 mb-1">
                  What you learned
                </p>
                <p className="text-xs text-gray-200 border-l-2 border-emerald-700 pl-2">
                  {entry.user_lesson}
                </p>
              </div>
            ) : (
              <LessonForm entryId={entry.id} onSaved={onChanged} />
            ))}
        </div>
      )}
    </div>
  )
}

export default function Journal() {
  const [entries, setEntries] = useState([])
  const [tab, setTab] = useState('action')
  const [loading, setLoading] = useState(true)

  const fetchEntries = async () => {
    setLoading(true)
    try {
      // The "action" and "all" tabs both fetch all entries; "open"/"closed"
      // delegate to the backend status filter.
      let url = `${API}/journal?limit=200`
      if (tab === 'open' || tab === 'closed') {
        url += `&status=${tab}`
      }
      const res = await axios.get(url)
      let data = res.data || []
      if (tab === 'action') {
        data = data.filter((e) => e.needs_action)
      }
      setEntries(data)
    } catch (err) {
      console.error('Failed to fetch journal:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchEntries()
  }, [tab])

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">Trade Journal</h1>
        <p className="text-sm text-gray-400 mt-1">
          The discipline log. The system captures the trade and the advisor's
          rationale automatically — you commit to your own thesis before
          executing, then write the lesson after the position closes. Over time,
          this is what builds calibration.
        </p>
      </div>

      <div className="flex gap-2 mb-4 border-b border-gray-800 overflow-x-auto">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap ${
              tab === t.id
                ? 'text-white border-b-2 border-indigo-500 -mb-px'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-gray-400 text-sm">Loading…</div>
      ) : entries.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center text-gray-400">
          <p className="text-sm">
            {tab === 'action'
              ? 'No entries need action right now.'
              : tab === 'open'
              ? 'No open trades.'
              : tab === 'closed'
              ? 'No closed trades yet.'
              : 'No journal entries yet.'}
          </p>
          <p className="text-xs mt-2 text-gray-500">
            Entries appear here when you place a trade via the Advisor's Execute
            button (with your thesis), or when the system detects a manual trade
            in your broker account.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {entries.map((e) => (
            <EntryCard key={e.id} entry={e} onChanged={fetchEntries} />
          ))}
        </div>
      )}
    </div>
  )
}
