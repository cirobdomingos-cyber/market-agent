import { useEffect, useState, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'

const API = '/api'
const REFRESH_MS = 30_000

const PERIODS = [
  { id: '1w', label: '1W', days: 7 },
  { id: '1m', label: '1M', days: 30 },
  { id: '3m', label: '3M', days: 90 },
  { id: '6m', label: '6M', days: 180 },
  { id: '1y', label: '1Y', days: 365 },
]

export default function Markets() {
  const [tickers, setTickers] = useState([])
  const [quotes, setQuotes] = useState({})
  const [loading, setLoading] = useState(true)
  const [selectedTicker, setSelectedTicker] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)

  const fetchWatchlist = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/watchlist`)
      const list = (res.data || []).map((r) => r.ticker).filter(Boolean)
      setTickers(list)
      if (list.length > 0 && !selectedTicker) {
        setSelectedTicker(list[0])
      }
      return list
    } catch (err) {
      console.error('Markets: failed to fetch watchlist', err)
      return []
    }
  }, [selectedTicker])

  const fetchQuotes = useCallback(async (list) => {
    if (!list || list.length === 0) return
    const results = await Promise.allSettled(
      list.map((t) =>
        axios
          .get(`${API}/market-data/${t}/quote`)
          .then((res) => ({ ticker: t, data: res.data }))
          .catch((err) => ({ ticker: t, error: err.response?.status || 'err' })),
      ),
    )
    const next = {}
    for (const r of results) {
      if (r.status === 'fulfilled') {
        next[r.value.ticker] = r.value.data ? { data: r.value.data } : { error: r.value.error }
      }
    }
    setQuotes((prev) => ({ ...prev, ...next }))
    setLastRefresh(new Date())
  }, [])

  useEffect(() => {
    let cancelled = false
    const refresh = async () => {
      const list = await fetchWatchlist()
      if (!cancelled) {
        await fetchQuotes(list)
        setLoading(false)
      }
    }
    refresh()
    const id = setInterval(refresh, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [fetchWatchlist, fetchQuotes])

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-4">
        <h1 className="text-2xl font-bold">Markets</h1>
        {lastRefresh && (
          <span className="text-xs text-gray-500">
            Updated {lastRefresh.toLocaleTimeString()} · auto-refresh 30s
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[360px_1fr] gap-4">
        <TickerList
          tickers={tickers}
          quotes={quotes}
          loading={loading}
          selectedTicker={selectedTicker}
          onSelect={setSelectedTicker}
        />
        <TickerDetail ticker={selectedTicker} quote={quotes[selectedTicker]?.data} />
      </div>
    </div>
  )
}

function TickerList({ tickers, quotes, loading, selectedTicker, onSelect }) {
  if (loading) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 text-gray-500">
        Loading watchlist…
      </div>
    )
  }
  if (tickers.length === 0) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 text-gray-500">
        Your watchlist is empty. Add tickers from the Portfolio page.
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 overflow-hidden">
      <div className="px-3 py-2 text-xs uppercase tracking-wide text-gray-500 border-b border-gray-800 grid grid-cols-[1fr_auto_auto] gap-3">
        <span>Ticker</span>
        <span className="text-right">Price</span>
        <span className="text-right w-16">Δ%</span>
      </div>
      <ul>
        {tickers.map((t) => (
          <TickerRow
            key={t}
            ticker={t}
            quote={quotes[t]}
            selected={t === selectedTicker}
            onClick={() => onSelect(t)}
          />
        ))}
      </ul>
    </div>
  )
}

function TickerRow({ ticker, quote, selected, onClick }) {
  const data = quote?.data
  const err = quote?.error
  const price = data?.price
  const pct = data?.change_percent
  const up = pct != null && pct >= 0
  const color = pct == null ? 'text-gray-400' : up ? 'text-emerald-400' : 'text-rose-400'

  return (
    <li
      onClick={onClick}
      className={`px-3 py-2 cursor-pointer border-b border-gray-800 last:border-b-0 grid grid-cols-[1fr_auto_auto] gap-3 items-center transition ${
        selected ? 'bg-indigo-900/40' : 'hover:bg-gray-800/60'
      }`}
    >
      <div>
        <div className="font-mono font-semibold">{ticker}</div>
        {data?.name && (
          <div className="text-xs text-gray-500 truncate max-w-[220px]">{data.name}</div>
        )}
      </div>
      <div className="text-right font-mono">
        {err ? (
          <span className="text-xs text-gray-500">err</span>
        ) : price == null ? (
          <span className="text-xs text-gray-500">…</span>
        ) : (
          `$${price.toFixed(2)}`
        )}
      </div>
      <div className={`text-right font-mono text-sm w-16 ${color}`}>
        {pct == null ? '' : `${up ? '+' : ''}${pct.toFixed(2)}%`}
      </div>
    </li>
  )
}

function TickerDetail({ ticker, quote }) {
  const navigate = useNavigate()
  const [period, setPeriod] = useState('3m')
  const [history, setHistory] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState(null)

  const days = PERIODS.find((p) => p.id === period)?.days ?? 90

  useEffect(() => {
    if (!ticker) return
    let cancelled = false
    const fetchHistory = async () => {
      setHistoryLoading(true)
      setHistoryError(null)
      try {
        const res = await axios.get(`${API}/market-data/${ticker}/history?days=${days}`)
        if (!cancelled) {
          setHistory(res.data.prices || [])
          setHistoryLoading(false)
        }
      } catch (err) {
        if (!cancelled) {
          setHistoryError(err.response?.data?.detail || 'Failed to load history')
          setHistoryLoading(false)
        }
      }
    }
    fetchHistory()
    return () => { cancelled = true }
  }, [ticker, days])

  const chartData = useMemo(() => {
    if (!history) return []
    return history.map((row) => ({ date: row.date, close: row.close }))
  }, [history])

  const up = quote?.change_percent != null && quote.change_percent >= 0
  const priceColor = quote?.change_percent == null ? 'text-gray-300' : up ? 'text-emerald-400' : 'text-rose-400'

  if (!ticker) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6 min-h-[400px] flex items-center justify-center text-gray-500">
        Select a ticker on the left.
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 sm:p-6 min-h-[400px] flex flex-col gap-4">
      {/* Header: ticker, name, price, change */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-mono text-2xl font-bold">{ticker}</div>
          {quote?.name && <div className="text-sm text-gray-400 truncate max-w-[300px]">{quote.name}</div>}
          {quote?.exchange && <div className="text-xs text-gray-500 mt-1">{quote.exchange}</div>}
        </div>
        <div className="text-right">
          <div className={`font-mono text-2xl font-bold ${priceColor}`}>
            {quote?.price != null ? `$${quote.price.toFixed(2)}` : '—'}
          </div>
          {quote?.change_percent != null && (
            <div className={`font-mono text-sm ${priceColor}`}>
              {up ? '+' : ''}{quote.change?.toFixed(2)} ({up ? '+' : ''}{quote.change_percent.toFixed(2)}%)
            </div>
          )}
        </div>
      </div>

      {/* Period selector */}
      <div className="flex gap-1">
        {PERIODS.map((p) => (
          <button
            key={p.id}
            onClick={() => setPeriod(p.id)}
            className={`px-3 py-1 text-xs font-mono rounded transition ${
              period === p.id
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Chart */}
      <div className="h-[280px]">
        {historyLoading && (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm">
            Loading chart…
          </div>
        )}
        {historyError && (
          <div className="h-full flex items-center justify-center text-rose-400 text-sm">
            {historyError}
          </div>
        )}
        {!historyLoading && !historyError && chartData.length > 0 && (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={up ? '#10b981' : '#f43f5e'} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={up ? '#10b981' : '#f43f5e'} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis
                dataKey="date"
                stroke="#6b7280"
                tick={{ fontSize: 11, fill: '#9ca3af' }}
                minTickGap={40}
              />
              <YAxis
                stroke="#6b7280"
                tick={{ fontSize: 11, fill: '#9ca3af' }}
                domain={['auto', 'auto']}
                tickFormatter={(v) => `$${v.toFixed(0)}`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#111827',
                  border: '1px solid #374151',
                  borderRadius: '6px',
                  fontSize: '12px',
                }}
                formatter={(v) => [`$${Number(v).toFixed(2)}`, 'Close']}
                labelStyle={{ color: '#9ca3af' }}
              />
              <Area
                type="monotone"
                dataKey="close"
                stroke={up ? '#10b981' : '#f43f5e'}
                strokeWidth={2}
                fill="url(#chartFill)"
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
        <Stat label="Volume" value={quote?.volume ? formatCompact(quote.volume) : '—'} />
        <Stat label="Market cap" value={quote?.market_cap ? formatCompact(quote.market_cap, '$') : '—'} />
        <Stat label="Currency" value={quote?.currency || '—'} />
        <Stat label="Last update" value={quote?.timestamp ? new Date(quote.timestamp).toLocaleTimeString() : '—'} />
      </div>

      {/* Action: Analyze this in Advisor */}
      <div className="pt-2 border-t border-gray-800">
        <button
          onClick={() =>
            navigate(
              `/advisor?prompt=${encodeURIComponent(
                `Analyze ${ticker}. Run full Layer 3–4 analysis for this specific asset in context of the current macro regime and sector rotation.`,
              )}`,
            )
          }
          className="px-4 py-2 text-sm bg-indigo-600 hover:bg-indigo-500 text-white rounded transition"
        >
          Analyze {ticker} in Advisor →
        </button>
      </div>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="font-mono text-gray-200">{value}</div>
    </div>
  )
}

function formatCompact(n, prefix = '') {
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e12) return `${prefix}${(n / 1e12).toFixed(2)}T`
  if (abs >= 1e9) return `${prefix}${(n / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${prefix}${(n / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${prefix}${(n / 1e3).toFixed(2)}K`
  return `${prefix}${n.toFixed(0)}`
}
