import { useEffect, useState } from 'react'
import axios from 'axios'

const API = '/api'
const REFRESH_MS = 60_000  // slower than Markets — trade perf doesn't change by the second

function fmtMoney(v, { signed = false } = {}) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const sign = signed && n > 0 ? '+' : ''
  return `${sign}$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtPct(v, { signed = false, decimals = 1 } = {}) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const sign = signed && n > 0 ? '+' : ''
  return `${sign}${n.toFixed(decimals)}%`
}

function colorForPnl(v) {
  if (v === null || v === undefined || v === 0) return 'text-gray-300'
  return v > 0 ? 'text-emerald-400' : 'text-rose-400'
}

export default function Performance() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const fetchData = async () => {
      try {
        const res = await axios.get(`${API}/performance`)
        if (!cancelled) {
          setData(res.data)
          setError(null)
          setLoading(false)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.response?.data?.detail || 'Failed to load performance data')
          setLoading(false)
        }
      }
    }
    fetchData()
    const id = setInterval(fetchData, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (loading) {
    return <p className="text-gray-500 text-sm">Loading performance...</p>
  }
  if (error) {
    return (
      <div className="border border-rose-800 rounded-lg p-4 text-rose-300 text-sm">
        {error}
      </div>
    )
  }
  if (!data) return null

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Performance</h1>
        <p className="text-sm text-gray-400 mt-1">
          Realised P&L on <span className="text-white font-semibold">live</span>{' '}
          trades only. Paper history is excluded — only real-money outcomes count here.
        </p>
      </div>

      <HeroKpiRow data={data} />
      <SecondaryKpiRow data={data} />
      <AdvisorAttributionCard data={data} />
      <RecentTradesTable trades={data.recent_trades} />
    </div>
  )
}

function HeroKpiRow({ data }) {
  const {
    win_rate_pct,
    total_realized_pnl,
    profit_factor,
    avg_hold_days,
    closed_trades_count,
    open_positions_count,
  } = data

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <KpiCard
        label="Win Rate"
        value={win_rate_pct === null ? '—' : fmtPct(win_rate_pct)}
        subtitle={
          closed_trades_count > 0
            ? `${data.winners_count}W / ${data.losers_count}L · ${closed_trades_count} closed`
            : `${closed_trades_count} closed trades`
        }
      />
      <KpiCard
        label="Realised P&L"
        value={fmtMoney(total_realized_pnl, { signed: true })}
        valueClassName={colorForPnl(total_realized_pnl)}
        subtitle={`${open_positions_count} open position${open_positions_count === 1 ? '' : 's'}`}
      />
      <KpiCard
        label="Profit Factor"
        value={profit_factor === null ? '—' : profit_factor.toFixed(2)}
        subtitle={
          profit_factor === null
            ? 'Need both winners and losers'
            : profit_factor >= 2
              ? 'Excellent'
              : profit_factor >= 1
                ? 'Profitable'
                : 'Losing'
        }
        valueClassName={
          profit_factor === null
            ? 'text-gray-300'
            : profit_factor >= 1
              ? 'text-emerald-400'
              : 'text-rose-400'
        }
      />
      <KpiCard
        label="Avg Hold"
        value={avg_hold_days === null ? '—' : `${avg_hold_days} days`}
        subtitle="Across closed trades"
      />
    </div>
  )
}

function SecondaryKpiRow({ data }) {
  const { largest_win, largest_loss, avg_win_dollar, avg_loss_dollar } = data

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
      <KpiCard
        label="Largest Win"
        value={fmtMoney(largest_win, { signed: true })}
        valueClassName="text-emerald-400"
        subtitle={null}
      />
      <KpiCard
        label="Largest Loss"
        value={fmtMoney(largest_loss, { signed: true })}
        valueClassName="text-rose-400"
        subtitle={null}
      />
      <KpiCard
        label="Avg Win / Avg Loss"
        value={
          avg_win_dollar && avg_loss_dollar
            ? `${fmtMoney(avg_win_dollar)} / ${fmtMoney(avg_loss_dollar)}`
            : '—'
        }
        subtitle={
          avg_win_dollar && avg_loss_dollar
            ? `Ratio ${(avg_win_dollar / Math.abs(avg_loss_dollar)).toFixed(2)}`
            : 'Need at least one of each'
        }
      />
    </div>
  )
}

function AdvisorAttributionCard({ data }) {
  const { advised_trades_count, advised_win_rate_pct, win_rate_pct } = data
  if (advised_trades_count === 0) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm text-gray-400">
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">
          Advisor attribution
        </div>
        No closed trades originated from an advisor chat yet. Trades executed
        via the Execute button on an advisor message will be tagged with
        their session ID and show up here.
      </div>
    )
  }

  const delta =
    advised_win_rate_pct !== null && win_rate_pct !== null
      ? advised_win_rate_pct - win_rate_pct
      : null

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
      <div className="text-xs uppercase tracking-wide text-gray-500 mb-2">
        Advisor attribution
      </div>
      <div className="flex items-baseline gap-6 flex-wrap">
        <div>
          <div className="text-2xl font-bold text-white">
            {fmtPct(advised_win_rate_pct)}
          </div>
          <div className="text-xs text-gray-500">
            win rate on {advised_trades_count} advised trade
            {advised_trades_count === 1 ? '' : 's'}
          </div>
        </div>
        {delta !== null && (
          <div>
            <div
              className={`text-lg font-semibold ${
                delta > 0.5
                  ? 'text-emerald-400'
                  : delta < -0.5
                    ? 'text-rose-400'
                    : 'text-gray-300'
              }`}
            >
              {delta > 0 ? '+' : ''}{delta.toFixed(1)} pts
            </div>
            <div className="text-xs text-gray-500">vs. overall</div>
          </div>
        )}
      </div>
    </div>
  )
}

function RecentTradesTable({ trades }) {
  if (!trades || trades.length === 0) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6 text-center text-gray-500 text-sm">
        No closed live trades yet. When your first bracket fills and exits,
        it'll appear here with P&L attribution.
      </div>
    )
  }
  return (
    <div>
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
        Recent closed trades
      </h2>
      <div className="border border-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-gray-400 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left px-4 py-2">Ticker</th>
              <th className="text-right px-4 py-2">Qty</th>
              <th className="text-right px-4 py-2">Entry</th>
              <th className="text-right px-4 py-2">Exit</th>
              <th className="text-right px-4 py-2">P&amp;L</th>
              <th className="text-right px-4 py-2">P&amp;L %</th>
              <th className="text-right px-4 py-2">Days</th>
              <th className="text-left px-4 py-2">Closed</th>
              <th className="text-left px-4 py-2">Source</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id} className="border-t border-gray-800">
                <td className="px-4 py-2 font-mono font-semibold">{t.ticker}</td>
                <td className="px-4 py-2 text-right font-mono">{t.qty}</td>
                <td className="px-4 py-2 text-right font-mono">
                  {fmtMoney(t.open_price)}
                </td>
                <td className="px-4 py-2 text-right font-mono">
                  {fmtMoney(t.close_price)}
                </td>
                <td className={`px-4 py-2 text-right font-mono ${colorForPnl(t.pnl_amount)}`}>
                  {fmtMoney(t.pnl_amount, { signed: true })}
                </td>
                <td className={`px-4 py-2 text-right font-mono ${colorForPnl(t.pnl_pct)}`}>
                  {fmtPct(t.pnl_pct, { signed: true, decimals: 2 })}
                </td>
                <td className="px-4 py-2 text-right font-mono text-gray-400">
                  {t.days_held ?? '—'}
                </td>
                <td className="px-4 py-2 text-xs text-gray-500">
                  {t.closed_at ? new Date(t.closed_at).toLocaleDateString() : '—'}
                </td>
                <td className="px-4 py-2 text-xs">
                  {t.advisor_session_id ? (
                    <span className="px-1.5 py-0.5 rounded bg-indigo-900/60 text-indigo-300 font-mono">
                      advisor
                    </span>
                  ) : (
                    <span className="text-gray-500">manual</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function KpiCard({ label, value, subtitle, valueClassName = 'text-white' }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-2xl font-bold mt-1 font-mono ${valueClassName}`}>
        {value}
      </div>
      {subtitle && (
        <div className="text-xs text-gray-500 mt-1">{subtitle}</div>
      )}
    </div>
  )
}
