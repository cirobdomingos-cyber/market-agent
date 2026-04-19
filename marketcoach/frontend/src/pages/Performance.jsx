import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import {
  AreaChart,
  Area,
  Line,
  Legend,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'

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
      <EquityCurveChart
        points={data.equity_curve}
        spyBenchmark={data.spy_benchmark}
      />
      <AdvisorAttributionCard data={data} />
      <RecentTradesTable trades={data.recent_trades} />
    </div>
  )
}

function EquityCurveChart({ points, spyBenchmark }) {
  // Merge the equity curve with the SPY benchmark by x-date.
  // spy_benchmark may be shorter than equity_curve (yfinance coverage
  // gaps, weekends outside the walk-back window), so indexing by
  // position would misalign. Matching by x keeps the two series honest.
  const chartData = useMemo(() => {
    if (!points || points.length === 0) return []
    const spyByX = Object.fromEntries(
      (spyBenchmark ?? []).map((p) => [p.x, p.cumulative_spy_pnl]),
    )
    return points.map((pt) => ({
      ...pt,
      cumulative_spy_pnl:
        spyByX[pt.x] !== undefined ? spyByX[pt.x] : null,
    }))
  }, [points, spyBenchmark])

  if (chartData.length === 0) {
    return null  // hero cards already show "0 closed trades" — don't double up
  }

  // Color the realised P&L series by its final sign. Matches the hero card.
  const final = chartData[chartData.length - 1].cumulative_pnl
  const isProfit = final >= 0
  const color = isProfit ? '#10b981' : '#f43f5e'

  const hasSpy = (spyBenchmark ?? []).length > 0
  const finalSpy = hasSpy
    ? spyBenchmark[spyBenchmark.length - 1].cumulative_spy_pnl
    : null
  // Alpha — did active trading beat same-notional buy-and-hold SPY over
  // the same windows? Positive = beating the index; negative = index
  // would have served better.
  const alpha = hasSpy && finalSpy !== null ? final - finalSpy : null

  return (
    <div>
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Cumulative realised P&amp;L
        </h2>
        {alpha !== null && (
          <span className="text-xs text-gray-500">
            <span className="font-medium text-gray-400">Alpha vs SPY: </span>
            <span
              className={`font-mono font-semibold ${
                alpha > 0.5
                  ? 'text-emerald-400'
                  : alpha < -0.5
                    ? 'text-rose-400'
                    : 'text-gray-300'
              }`}
            >
              {alpha >= 0 ? '+' : ''}${alpha.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            </span>
          </span>
        )}
      </div>
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 h-[300px]">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.3} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis
              dataKey="x"
              stroke="#6b7280"
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              minTickGap={40}
              tickFormatter={(iso) => new Date(iso).toLocaleDateString()}
            />
            <YAxis
              stroke="#6b7280"
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              tickFormatter={(v) => `$${v.toLocaleString()}`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#111827',
                border: '1px solid #374151',
                borderRadius: '6px',
                fontSize: '12px',
              }}
              labelFormatter={(iso) => new Date(iso).toLocaleString()}
              formatter={(value, name, props) => {
                // Recharts calls the formatter once per series per point.
                // Distinct labels per series so the tooltip shows both
                // lines with clear attribution.
                const p = props.payload
                if (!p) return ['—', name]
                if (name === 'Realised P&L') {
                  return [
                    `$${p.cumulative_pnl.toLocaleString()} (${
                      p.trade_pnl >= 0 ? '+' : ''
                    }$${p.trade_pnl.toLocaleString()} on ${p.ticker})`,
                    'Realised P&L',
                  ]
                }
                if (name === 'SPY (same capital)') {
                  if (p.cumulative_spy_pnl === null || p.cumulative_spy_pnl === undefined) {
                    return ['—', name]
                  }
                  return [
                    `$${p.cumulative_spy_pnl.toLocaleString()}`,
                    'SPY (same capital)',
                  ]
                }
                return [value, name]
              }}
              labelStyle={{ color: '#9ca3af' }}
            />
            {hasSpy && (
              <Legend
                verticalAlign="top"
                height={24}
                wrapperStyle={{ fontSize: '11px', color: '#9ca3af' }}
                iconSize={10}
              />
            )}
            <ReferenceLine y={0} stroke="#374151" strokeDasharray="3 3" />
            <Area
              name="Realised P&L"
              type="monotone"
              dataKey="cumulative_pnl"
              stroke={color}
              strokeWidth={2}
              fill="url(#pnlFill)"
            />
            {hasSpy && (
              <Line
                name="SPY (same capital)"
                type="monotone"
                dataKey="cumulative_spy_pnl"
                stroke="#9ca3af"
                strokeWidth={1.5}
                strokeDasharray="4 4"
                dot={false}
                connectNulls
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <p className="text-xs text-gray-500 mt-2">
        Each point is a closed live trade. Line steps when P&amp;L realises —
        open positions don't move it until they close.
        {hasSpy && (
          <>
            {' '}The dashed grey line is what the same dollar notional would
            have earned held in SPY from each trade's open to close.
          </>
        )}
      </p>
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
