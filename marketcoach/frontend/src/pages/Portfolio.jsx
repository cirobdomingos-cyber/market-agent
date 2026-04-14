import { useState, useEffect } from 'react'
import axios from 'axios'

const API = '/api'

export default function Portfolio() {
  const [portfolio, setPortfolio] = useState(null)
  const [orders, setOrders] = useState([])
  const [accuracy, setAccuracy] = useState(null)
  const [watchlist, setWatchlist] = useState([])
  const [newTicker, setNewTicker] = useState('')
  const [newNotes, setNewNotes] = useState('')
  const [watchlistError, setWatchlistError] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchWatchlist = async () => {
    try {
      const res = await axios.get(`${API}/watchlist`)
      setWatchlist(res.data || [])
    } catch (err) {
      console.error('Failed to fetch watchlist:', err)
    }
  }

  const addTicker = async () => {
    const t = newTicker.trim().toUpperCase()
    if (!t) return
    setWatchlistError(null)
    try {
      await axios.post(`${API}/watchlist`, {
        ticker: t,
        notes: newNotes.trim() || null,
      })
      setNewTicker('')
      setNewNotes('')
      fetchWatchlist()
    } catch (err) {
      const detail = err.response?.data?.detail
      setWatchlistError(
        Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
          : detail || 'Failed to add ticker'
      )
    }
  }

  const removeTicker = async (ticker) => {
    if (!confirm(`Remove ${ticker} from your watchlist?`)) return
    try {
      await axios.delete(`${API}/watchlist/${ticker}`)
      fetchWatchlist()
    } catch (err) {
      console.error('Failed to remove ticker:', err)
    }
  }

  useEffect(() => {
    let cancelled = false
    async function fetchData() {
      try {
        const [portRes, ordRes, accRes, wlRes] = await Promise.all([
          axios.get(`${API}/portfolio`).catch(() => ({ data: null })),
          axios.get(`${API}/portfolio/orders`).catch(() => ({ data: [] })),
          axios.get(`${API}/accuracy`),
          axios.get(`${API}/watchlist`).catch(() => ({ data: [] })),
        ])
        if (cancelled) return
        setPortfolio(portRes.data)
        setOrders(ordRes.data || [])
        setAccuracy(accRes.data)
        setWatchlist(wlRes.data || [])
      } catch (err) {
        console.error('Failed to load portfolio:', err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchData()
    // Auto-refresh every 15s so position changes appear without manual reload.
    // 15s is the sweet spot: fast enough to feel live after a trade, slow
    // enough to not hammer IBKR's socket.
    const id = setInterval(fetchData, 15_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (loading) return <p className="text-gray-500 text-sm">Loading portfolio...</p>

  const account = portfolio?.account
  const positions = portfolio?.positions || []
  const isConnected = account && account.status !== 'disconnected'

  return (
    <div className="space-y-6">
      {/* Account Summary */}
      {isConnected && (
        <div className="grid grid-cols-4 gap-4">
          {[
            { label: 'Portfolio Value', value: `$${Number(account.portfolio_value).toLocaleString()}` },
            { label: 'Equity', value: `$${Number(account.equity).toLocaleString()}` },
            { label: 'Buying Power', value: `$${Number(account.buying_power).toLocaleString()}` },
            { label: 'Cash', value: `$${Number(account.cash).toLocaleString()}` },
          ].map((item) => (
            <div key={item.label} className="border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500 uppercase">{item.label}</p>
              <p className="text-lg font-bold text-white mt-1">{item.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Positions */}
      <div>
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
          Paper Positions ({positions.length})
        </h2>
        {!isConnected ? (
          <div className="border border-gray-800 rounded-lg p-6 text-center">
            <p className="text-gray-400 text-sm">
              Alpaca not connected. Add <code className="text-indigo-400">ALPACA_API_KEY</code> and{' '}
              <code className="text-indigo-400">ALPACA_SECRET_KEY</code> to .env.
            </p>
            <p className="text-gray-500 text-xs mt-2">
              Get free paper trading keys at{' '}
              <a href="https://alpaca.markets" className="text-indigo-400 hover:underline" target="_blank" rel="noreferrer">
                alpaca.markets
              </a>
            </p>
          </div>
        ) : positions.length > 0 ? (
          <div className="border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-900 text-gray-400">
                <tr>
                  <th className="text-left px-4 py-2">Ticker</th>
                  <th className="text-right px-4 py-2">Qty</th>
                  <th className="text-right px-4 py-2">Avg Entry</th>
                  <th className="text-right px-4 py-2">Current</th>
                  <th className="text-right px-4 py-2">P&L</th>
                  <th className="text-right px-4 py-2">P&L %</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i} className="border-t border-gray-800">
                    <td className="px-4 py-2 font-mono font-bold">{p.ticker}</td>
                    <td className="px-4 py-2 text-right">{p.qty}</td>
                    <td className="px-4 py-2 text-right">${Number(p.avg_entry).toFixed(2)}</td>
                    <td className="px-4 py-2 text-right">${Number(p.current_price).toFixed(2)}</td>
                    <td className={`px-4 py-2 text-right font-medium ${
                      p.unrealised_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}>
                      ${Number(p.unrealised_pnl).toFixed(2)}
                    </td>
                    <td className={`px-4 py-2 text-right ${
                      p.unrealised_pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}>
                      {Number(p.unrealised_pnl_pct).toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-gray-500 text-sm">No open positions.</p>
        )}
      </div>

      {/* Watchlist */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
            Watchlist ({watchlist.length})
          </h2>
          <p className="text-xs text-gray-500">
            Tickers here can be traded via the Execute button. Also seeds news pipeline.
          </p>
        </div>

        {/* Add-ticker row */}
        <div className="border border-gray-800 rounded-lg p-3 mb-3 flex gap-2 items-start flex-wrap">
          <input
            type="text"
            value={newTicker}
            onChange={(e) => setNewTicker(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === 'Enter' && addTicker()}
            placeholder="TICKER"
            maxLength={5}
            className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-sm font-mono focus:outline-none focus:border-indigo-500"
          />
          <input
            type="text"
            value={newNotes}
            onChange={(e) => setNewNotes(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addTicker()}
            placeholder="Optional notes — why you're watching this"
            maxLength={500}
            className="flex-1 min-w-[180px] bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-sm focus:outline-none focus:border-indigo-500"
          />
          <button
            onClick={addTicker}
            disabled={!newTicker.trim()}
            className="px-3 py-1.5 text-sm bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-500 rounded font-medium transition-colors"
          >
            Add
          </button>
        </div>
        {watchlistError && (
          <p className="text-xs text-red-300 mb-2">{watchlistError}</p>
        )}

        {/* Ticker grid */}
        {watchlist.length === 0 ? (
          <p className="text-gray-500 text-sm">Watchlist is empty.</p>
        ) : (
          <div className="border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-900 text-gray-400">
                <tr>
                  <th className="text-left px-4 py-2 w-24">Ticker</th>
                  <th className="text-left px-4 py-2">Notes</th>
                  <th className="text-right px-4 py-2 w-24">Added</th>
                  <th className="text-right px-4 py-2 w-16"></th>
                </tr>
              </thead>
              <tbody>
                {watchlist.map((w) => (
                  <tr key={w.ticker} className="border-t border-gray-800">
                    <td className="px-4 py-2 font-mono font-bold text-white">{w.ticker}</td>
                    <td className="px-4 py-2 text-gray-400">
                      {w.notes || <span className="text-gray-700 italic">—</span>}
                    </td>
                    <td className="px-4 py-2 text-right text-xs text-gray-500">
                      {w.added_at ? new Date(w.added_at).toLocaleDateString() : ''}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        onClick={() => removeTicker(w.ticker)}
                        title={`Remove ${w.ticker}`}
                        className="text-xs text-gray-500 hover:text-red-400 transition-colors"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Order History */}
      {orders.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
            Recent Orders
          </h2>
          <div className="border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-900 text-gray-400">
                <tr>
                  <th className="text-left px-4 py-2">Ticker</th>
                  <th className="text-left px-4 py-2">Side</th>
                  <th className="text-right px-4 py-2">Qty</th>
                  <th className="text-right px-4 py-2">Fill Price</th>
                  <th className="text-left px-4 py-2">Status</th>
                  <th className="text-left px-4 py-2">Time</th>
                </tr>
              </thead>
              <tbody>
                {orders.slice(0, 10).map((o, i) => (
                  <tr key={i} className="border-t border-gray-800">
                    <td className="px-4 py-2 font-mono font-bold">{o.ticker}</td>
                    <td className={`px-4 py-2 ${o.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                      {o.side?.toUpperCase()}
                    </td>
                    <td className="px-4 py-2 text-right">{o.filled_qty || o.qty}</td>
                    <td className="px-4 py-2 text-right">
                      {o.filled_avg_price ? `$${Number(o.filled_avg_price).toFixed(2)}` : '--'}
                    </td>
                    <td className="px-4 py-2 text-gray-400">{o.status}</td>
                    <td className="px-4 py-2 text-gray-500 text-xs">
                      {o.submitted_at ? new Date(o.submitted_at).toLocaleString() : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Thesis Accuracy */}
      <div>
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
          Thesis Accuracy
        </h2>
        {accuracy && accuracy.total > 0 ? (
          <div className="border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-6">
              <div className="text-center">
                <span className="text-3xl font-bold text-indigo-400">{accuracy.accuracy_pct}%</span>
                <p className="text-gray-500 text-xs mt-1">Overall</p>
              </div>
              <div className="flex-1 grid grid-cols-3 gap-4 text-sm text-center">
                <div>
                  <p className="text-white font-medium">{accuracy.total}</p>
                  <p className="text-gray-500 text-xs">Resolved</p>
                </div>
                <div>
                  <p className="text-green-400 font-medium">{accuracy.correct}</p>
                  <p className="text-gray-500 text-xs">Correct</p>
                </div>
                <div>
                  <p className="text-red-400 font-medium">{accuracy.incorrect}</p>
                  <p className="text-gray-500 text-xs">Incorrect</p>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <p className="text-gray-500 text-sm">Run the pipeline to generate theses. They auto-resolve when expired.</p>
        )}
      </div>
    </div>
  )
}
