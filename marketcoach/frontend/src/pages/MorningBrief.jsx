import { useState, useEffect } from 'react'
import axios from 'axios'
import ReactMarkdown from 'react-markdown'

const API = '/api'

function formatTimestamp(iso) {
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

export default function MorningBrief() {
  const [briefs, setBriefs] = useState([])
  const [expandedId, setExpandedId] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchBriefs = async () => {
    setLoading(true)
    try {
      const res = await axios.get(`${API}/morning-brief?limit=30`)
      setBriefs(res.data || [])
      // Auto-expand the most recent one so the archive page doubles as
      // a quick "latest brief" view when opened from the Dashboard link.
      if (res.data && res.data.length > 0) {
        setExpandedId(res.data[0].id)
      }
    } catch (err) {
      console.error('Failed to fetch morning briefs:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchBriefs()
  }, [])

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">Morning Brief Archive</h1>
        <p className="text-sm text-gray-400 mt-1">
          Auto-generated every weekday at 06:00 local — overnight news, today's
          earnings, premarket movers, and action items.
        </p>
      </div>

      {loading ? (
        <div className="text-gray-400">Loading…</div>
      ) : briefs.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-center text-gray-400">
          <p>No morning briefs yet.</p>
          <p className="text-sm mt-2 text-gray-500">
            The next scheduled run is the next weekday at 06:00. Use the Dashboard's
            <strong className="text-gray-300"> Run now</strong> button to generate one immediately.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {briefs.map((b) => {
            const expanded = expandedId === b.id
            const isFailed = b.status === 'failed'
            return (
              <div
                key={b.id}
                onClick={() => setExpandedId(expanded ? null : b.id)}
                className="bg-gray-900 border border-gray-800 hover:border-gray-700 rounded-lg p-4 cursor-pointer transition-colors"
              >
                <div className="flex items-center justify-between gap-3 mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-white font-medium text-sm">
                      {formatTimestamp(b.created_at)}
                    </span>
                    {b.trigger === 'manual' && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                        manual
                      </span>
                    )}
                    {isFailed && (
                      <span className="text-xs text-red-400">FAILED</span>
                    )}
                  </div>
                  <span className="text-xs text-gray-500">
                    {expanded ? '▲' : '▼'}
                  </span>
                </div>

                {expanded && (
                  <div className="mt-3 pt-3 border-t border-gray-800">
                    {isFailed ? (
                      <div className="text-sm text-red-300">
                        Generation failed: {b.error || 'unknown error'}
                      </div>
                    ) : (
                      <div className="prose prose-invert prose-sm max-w-none">
                        <ReactMarkdown>{b.content}</ReactMarkdown>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
