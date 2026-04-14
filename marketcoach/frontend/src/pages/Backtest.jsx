import { useState, useEffect } from 'react'
import axios from 'axios'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, AreaChart, Area, ReferenceLine,
} from 'recharts'

const API = '/api'

// Default date helpers
function daysAgo(n) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toISOString().split('T')[0]
}

// Metric card component
function MetricCard({ label, value, sub, color = 'text-white' }) {
  return (
    <div className="border border-gray-800 rounded-lg p-4 text-center">
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
      <p className="text-gray-400 text-xs mt-1">{label}</p>
      {sub && <p className="text-gray-500 text-xs mt-0.5">{sub}</p>}
    </div>
  )
}

export default function Backtest() {
  // Config state
  const [startDate, setStartDate] = useState(daysAgo(30))
  const [endDate, setEndDate] = useState(daysAgo(1))
  const [capital, setCapital] = useState(100000)
  const [interval, setInterval_] = useState(7)
  const [risk, setRisk] = useState('moderate')
  const [watchlist, setWatchlist] = useState('AAPL,MSFT,NVDA,GOOGL,AMZN,TSLA,META,SPY,QQQ')

  // Result state
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const [pastRuns, setPastRuns] = useState([])

  // Tab state
  const [activeTab, setActiveTab] = useState('equity')

  // Fetch past runs on mount
  useEffect(() => {
    axios.get(`${API}/backtests?limit=10`).then(res => setPastRuns(res.data)).catch(() => {})
  }, [])

  const runBacktest = async () => {
    setRunning(true)
    setError(null)
    setResult(null)

    try {
      const res = await axios.post(`${API}/backtest/run`, {
        start_date: startDate,
        end_date: endDate,
        initial_capital: capital,
        watchlist: watchlist.split(',').map(t => t.trim()).filter(Boolean),
        decision_interval_days: interval,
        risk_tolerance: risk,
      })
      setResult(res.data)
      // Refresh past runs
      const runsRes = await axios.get(`${API}/backtests?limit=10`)
      setPastRuns(runsRes.data)
    } catch (err) {
      const msg = err.response?.data?.detail || err.message || 'Backtest failed'
      setError(msg)
    } finally {
      setRunning(false)
    }
  }

  const loadPastRun = async (id) => {
    try {
      const res = await axios.get(`${API}/backtest/${id}`)
      setResult(res.data)
    } catch (err) {
      setError('Failed to load backtest')
    }
  }

  const m = result?.metrics

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-bold">Time-Machine Backtest</h1>

      {/* Config form */}
      <div className="border border-gray-800 rounded-lg p-5 space-y-4">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Configuration
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Start Date</label>
            <input
              type="date"
              value={startDate}
              onChange={e => setStartDate(e.target.value)}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">End Date</label>
            <input
              type="date"
              value={endDate}
              onChange={e => setEndDate(e.target.value)}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Capital ($)</label>
            <input
              type="number"
              value={capital}
              onChange={e => setCapital(Number(e.target.value))}
              min={1000}
              max={10000000}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Decision Interval</label>
            <select
              value={interval}
              onChange={e => setInterval_(Number(e.target.value))}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            >
              <option value={3}>Every 3 days</option>
              <option value={5}>Every 5 days</option>
              <option value={7}>Weekly</option>
              <option value={14}>Bi-weekly</option>
              <option value={30}>Monthly</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Risk Tolerance</label>
            <select
              value={risk}
              onChange={e => setRisk(e.target.value)}
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            >
              <option value="conservative">Conservative</option>
              <option value="moderate">Moderate</option>
              <option value="aggressive">Aggressive</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Watchlist</label>
            <input
              type="text"
              value={watchlist}
              onChange={e => setWatchlist(e.target.value)}
              placeholder="AAPL,MSFT,NVDA..."
              className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
            />
          </div>
        </div>

        <div className="flex items-center gap-4">
          <button
            onClick={runBacktest}
            disabled={running}
            className="px-6 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40
                       rounded text-sm font-medium transition-colors"
          >
            {running ? 'Running backtest...' : 'Run Backtest'}
          </button>
          {running && (
            <span className="text-gray-400 text-sm animate-pulse">
              This may take 1-3 minutes (Claude is analyzing each decision point)...
            </span>
          )}
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Results section */}
      {result && m && (
        <>
          {/* Metrics summary */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
            <MetricCard
              label="Total Return"
              value={`${m.total_return_pct >= 0 ? '+' : ''}${m.total_return_pct}%`}
              color={m.total_return_pct >= 0 ? 'text-green-400' : 'text-red-400'}
              sub={`$${m.initial_capital.toLocaleString()} → $${m.final_equity.toLocaleString()}`}
            />
            <MetricCard label="Total Trades" value={m.total_trades} />
            <MetricCard
              label="Win Rate"
              value={`${m.win_rate_pct}%`}
              color={m.win_rate_pct >= 50 ? 'text-green-400' : 'text-yellow-400'}
              sub={`${m.winning_trades}W / ${m.losing_trades}L`}
            />
            <MetricCard
              label="Avg Win"
              value={`+${m.avg_win_pct}%`}
              color="text-green-400"
            />
            <MetricCard
              label="Avg Loss"
              value={`${m.avg_loss_pct}%`}
              color="text-red-400"
            />
            <MetricCard
              label="Max Drawdown"
              value={`-${m.max_drawdown_pct}%`}
              color="text-red-400"
            />
            <MetricCard
              label="Sharpe Ratio"
              value={m.sharpe_ratio != null ? m.sharpe_ratio : 'N/A'}
              color={m.sharpe_ratio > 1 ? 'text-green-400' : m.sharpe_ratio > 0 ? 'text-yellow-400' : 'text-red-400'}
            />
            <MetricCard
              label="Profit Factor"
              value={m.profit_factor != null ? m.profit_factor : 'N/A'}
              color={m.profit_factor > 1 ? 'text-green-400' : 'text-red-400'}
            />
          </div>

          {/* Tab navigation */}
          <div className="flex gap-4 border-b border-gray-800">
            {['equity', 'trades', 'drawdown'].map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`pb-2 px-1 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab
                    ? 'border-indigo-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-white'
                }`}
              >
                {tab === 'equity' ? 'Equity Curve' : tab === 'trades' ? 'Trade Log' : 'Drawdown'}
              </button>
            ))}
          </div>

          {/* Equity curve chart */}
          {activeTab === 'equity' && result.equity_curve?.length > 0 && (
            <div className="border border-gray-800 rounded-lg p-4">
              <ResponsiveContainer width="100%" height={400}>
                <AreaChart data={result.equity_curve}>
                  <defs>
                    <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis
                    dataKey="date"
                    tick={{ fill: '#9ca3af', fontSize: 11 }}
                    tickFormatter={d => d.slice(5)} /* MM-DD */
                  />
                  <YAxis
                    tick={{ fill: '#9ca3af', fontSize: 11 }}
                    tickFormatter={v => `$${(v / 1000).toFixed(0)}k`}
                  />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#111827', border: '1px solid #374151', borderRadius: 8 }}
                    labelStyle={{ color: '#9ca3af' }}
                    formatter={(v) => [`$${Number(v).toLocaleString()}`, 'Equity']}
                  />
                  <ReferenceLine
                    y={m.initial_capital}
                    stroke="#6b7280"
                    strokeDasharray="3 3"
                    label={{ value: 'Start', fill: '#6b7280', fontSize: 11 }}
                  />
                  <Area
                    type="monotone"
                    dataKey="equity"
                    stroke="#6366f1"
                    fill="url(#equityGrad)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Drawdown chart */}
          {activeTab === 'drawdown' && result.equity_curve?.length > 0 && (
            <div className="border border-gray-800 rounded-lg p-4">
              <ResponsiveContainer width="100%" height={300}>
                <AreaChart data={result.equity_curve}>
                  <defs>
                    <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#ef4444" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis
                    dataKey="date"
                    tick={{ fill: '#9ca3af', fontSize: 11 }}
                    tickFormatter={d => d.slice(5)}
                  />
                  <YAxis
                    tick={{ fill: '#9ca3af', fontSize: 11 }}
                    tickFormatter={v => `-${v}%`}
                  />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#111827', border: '1px solid #374151', borderRadius: 8 }}
                    formatter={(v) => [`-${v}%`, 'Drawdown']}
                  />
                  <Area
                    type="monotone"
                    dataKey="drawdown_pct"
                    stroke="#ef4444"
                    fill="url(#ddGrad)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Trade log table */}
          {activeTab === 'trades' && (
            <div className="border border-gray-800 rounded-lg overflow-hidden">
              <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-900/50 sticky top-0">
                    <tr className="text-gray-400 text-xs uppercase tracking-wider">
                      <th className="px-4 py-3 text-left">Ticker</th>
                      <th className="px-4 py-3 text-left">Direction</th>
                      <th className="px-4 py-3 text-left">Entry</th>
                      <th className="px-4 py-3 text-left">Exit</th>
                      <th className="px-4 py-3 text-right">Entry $</th>
                      <th className="px-4 py-3 text-right">Exit $</th>
                      <th className="px-4 py-3 text-right">P&L</th>
                      <th className="px-4 py-3 text-right">Return</th>
                      <th className="px-4 py-3 text-left">Exit Reason</th>
                      <th className="px-4 py-3 text-right">Confidence</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800">
                    {result.trades?.map((t, i) => (
                      <tr key={i} className="hover:bg-gray-900/30">
                        <td className="px-4 py-3 font-medium">{t.ticker}</td>
                        <td className="px-4 py-3">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                            t.direction === 'long'
                              ? 'bg-green-900/40 text-green-400'
                              : 'bg-red-900/40 text-red-400'
                          }`}>
                            {t.direction.toUpperCase()}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-gray-400">{t.entry_date}</td>
                        <td className="px-4 py-3 text-gray-400">{t.exit_date || '-'}</td>
                        <td className="px-4 py-3 text-right">${t.entry_price.toFixed(2)}</td>
                        <td className="px-4 py-3 text-right">${t.exit_price?.toFixed(2) || '-'}</td>
                        <td className={`px-4 py-3 text-right font-medium ${
                          t.pnl_dollars >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {t.pnl_dollars >= 0 ? '+' : ''}${t.pnl_dollars.toLocaleString()}
                        </td>
                        <td className={`px-4 py-3 text-right font-medium ${
                          t.pnl_percent >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {t.pnl_percent >= 0 ? '+' : ''}{t.pnl_percent}%
                        </td>
                        <td className="px-4 py-3">
                          <span className={`px-2 py-0.5 rounded text-xs ${
                            t.exit_reason === 'take_profit' ? 'bg-green-900/30 text-green-400' :
                            t.exit_reason === 'stop_loss' ? 'bg-red-900/30 text-red-400' :
                            'bg-gray-800 text-gray-400'
                          }`}>
                            {t.exit_reason.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right text-gray-400">{(t.confidence * 100).toFixed(0)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Run info */}
          <div className="text-xs text-gray-500 flex gap-4">
            <span>Decision points: {result.decision_points}</span>
            <span>Theses generated: {result.total_theses_generated}</span>
            <span>Duration: {
              ((new Date(result.completed_at) - new Date(result.started_at)) / 1000).toFixed(0)
            }s</span>
          </div>
        </>
      )}

      {/* Past runs */}
      {pastRuns.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
            Past Backtests
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {pastRuns.map(run => (
              <button
                key={run.id}
                onClick={() => loadPastRun(run.id)}
                className="border border-gray-800 rounded-lg p-4 text-left hover:border-indigo-600
                           transition-colors space-y-2"
              >
                <div className="flex items-center justify-between">
                  <span className={`text-lg font-bold ${
                    run.total_return_pct >= 0 ? 'text-green-400' : 'text-red-400'
                  }`}>
                    {run.total_return_pct >= 0 ? '+' : ''}{run.total_return_pct}%
                  </span>
                  <span className="text-xs text-gray-500">
                    {new Date(run.completed_at).toLocaleDateString()}
                  </span>
                </div>
                <div className="flex gap-3 text-xs text-gray-400">
                  <span>{run.total_trades} trades</span>
                  <span>Win: {run.win_rate_pct}%</span>
                  <span>DD: -{run.max_drawdown_pct}%</span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
