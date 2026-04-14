import { useState, useEffect } from 'react'
import axios from 'axios'

const API = '/api'

const STATUS_TABS = [
  { id: null, label: 'All' },
  { id: 'filled', label: 'Filled' },
  { id: 'accepted', label: 'Pending' },
  { id: 'rejected', label: 'Rejected' },
  { id: 'failed', label: 'Failed' },
]

const STATUS_STYLES = {
  filled: 'bg-emerald-900/40 border-emerald-700 text-emerald-200',
  accepted: 'bg-blue-900/40 border-blue-700 text-blue-200',
  rejected: 'bg-amber-900/40 border-amber-700 text-amber-200',
  failed: 'bg-red-900/40 border-red-700 text-red-200',
}

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function formatTimestamp(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function ExecutedOrders() {
  const [orders, setOrders] = useState([])
  const [statusFilter, setStatusFilter] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchOrders = async (filter) => {
    setLoading(true)
    try {
      const params = filter ? `?status=${filter}` : ''
      const res = await axios.get(`${API}/orders/executed${params}`)
      setOrders(res.data || [])
    } catch (err) {
      console.error('Failed to fetch executed orders:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchOrders(statusFilter)
  }, [statusFilter])

  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">Executed Orders</h1>
        <p className="text-sm text-gray-400 mt-1">
          Audit trail of every order MarketCoach placed on your behalf — including
          orders that the safety gates rejected before reaching Alpaca.
        </p>
      </div>

      <div className="flex gap-2 mb-4 border-b border-gray-800 overflow-x-auto">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.id || 'all'}
            onClick={() => setStatusFilter(tab.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap ${
              statusFilter === tab.id
                ? 'text-white border-b-2 border-indigo-500 -mb-px'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-gray-400 text-sm">Loading…</div>
      ) : orders.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center text-gray-400">
          <p>No orders in this view.</p>
          <p className="text-xs mt-2 text-gray-500">
            Orders appear here when you click <strong>Execute</strong> on a trade
            proposal in the Advisor chat.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {orders.map((o) => (
            <div
              key={o.id}
              className="bg-gray-900 border border-gray-800 rounded-lg p-4"
            >
              <div className="flex items-start justify-between gap-3 mb-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className={`text-xs font-bold px-2 py-0.5 rounded text-white ${
                      o.side === 'buy' ? 'bg-emerald-700' : 'bg-red-700'
                    }`}
                  >
                    {o.side.toUpperCase()}
                  </span>
                  <span className="text-white font-bold">{o.ticker}</span>
                  <span className="text-sm text-gray-300">
                    {o.qty} @ {o.order_type}
                    {o.limit_price ? ` ${fmtMoney(o.limit_price)}` : ''}
                  </span>
                  <span
                    className={`text-xs px-2 py-0.5 rounded border ${
                      STATUS_STYLES[o.status] || 'bg-gray-800 border-gray-700 text-gray-300'
                    }`}
                  >
                    {o.status.toUpperCase()}
                  </span>
                  {!o.is_paper && (
                    <span className="text-xs px-2 py-0.5 rounded bg-red-900/50 border border-red-700 text-red-200 font-bold">
                      LIVE
                    </span>
                  )}
                </div>
                <span className="text-xs text-gray-500 shrink-0">
                  {formatTimestamp(o.created_at)}
                </span>
              </div>

              {o.fill_price && (
                <div className="text-xs text-gray-400 mb-1">
                  Filled at <span className="text-white font-mono">{fmtMoney(o.fill_price)}</span>
                  {o.filled_at && ` · ${formatTimestamp(o.filled_at)}`}
                </div>
              )}

              {o.rejection_reason && (
                <div className="text-xs text-amber-300 mt-1">
                  ⚠ {o.rejection_reason}
                </div>
              )}

              {o.rationale && (
                <p className="text-xs text-gray-400 italic mt-2 border-l-2 border-gray-700 pl-2">
                  "{o.rationale}"
                </p>
              )}

              {o.alpaca_order_id && (
                <p className="text-[10px] text-gray-600 mt-2 font-mono">
                  Alpaca id: {o.alpaca_order_id}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
