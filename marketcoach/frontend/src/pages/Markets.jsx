import { useEffect, useState, useCallback } from 'react'
import axios from 'axios'

const API = '/api'
const REFRESH_MS = 30_000

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
        <DetailPlaceholder ticker={selectedTicker} />
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

function DetailPlaceholder({ ticker }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6 min-h-[400px] flex items-center justify-center text-gray-500">
      {ticker ? (
        <div className="text-center">
          <div className="text-xl font-mono text-gray-300 mb-2">{ticker}</div>
          <div className="text-sm">Chart + stats coming next commit.</div>
        </div>
      ) : (
        <div>Select a ticker on the left.</div>
      )}
    </div>
  )
}
