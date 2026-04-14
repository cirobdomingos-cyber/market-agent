import { useState, useEffect } from 'react'
import axios from 'axios'

const API = '/api'

export default function Portfolio() {
  const [portfolio, setPortfolio] = useState(null)
  const [orders, setOrders] = useState([])
  const [accuracy, setAccuracy] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function fetchData() {
      try {
        const [portRes, ordRes, accRes] = await Promise.all([
          axios.get(`${API}/portfolio`).catch(() => ({ data: null })),
          axios.get(`${API}/portfolio/orders`).catch(() => ({ data: [] })),
          axios.get(`${API}/accuracy`),
        ])
        setPortfolio(portRes.data)
        setOrders(ordRes.data || [])
        setAccuracy(accRes.data)
      } catch (err) {
        console.error('Failed to load portfolio:', err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
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
