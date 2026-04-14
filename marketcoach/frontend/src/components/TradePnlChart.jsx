import { useMemo } from 'react'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Dot,
} from 'recharts'

/**
 * Trade-by-trade realized P&L chart + stat cards.
 *
 * Complements the time-based EquityChart on the Dashboard: where that
 * shows "account equity over time every 5 min", this shows "which
 * closed trades actually paid off." Event-based (one point per
 * closed trade), realized-only (open positions don't appear),
 * derived client-side from the same /journal endpoint the list view
 * already uses.
 *
 * Takes a `closedEntries` prop so the parent (Journal page) can share
 * data between the chart and the entry list without a second fetch.
 */

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtSigned(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}

/** Coloured dot — emerald for winners, red for losers. */
function PnlDot(props) {
  const { cx, cy, payload } = props
  if (cx == null || cy == null) return null
  const color = payload.pnl >= 0 ? '#34d399' : '#f87171'
  return <circle cx={cx} cy={cy} r={4} fill={color} stroke={color} />
}

function StatCard({ label, value, color = 'text-white', small }) {
  return (
    <div className="border border-gray-800 rounded-lg p-3">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-lg font-bold mt-1 ${color}`}>{value}</p>
      {small && <p className="text-[10px] text-gray-600 mt-0.5">{small}</p>}
    </div>
  )
}

export default function TradePnlChart({ closedEntries }) {
  const { chartData, stats } = useMemo(() => {
    const valid = (closedEntries || [])
      .filter((e) => e.pnl_amount !== null && e.pnl_amount !== undefined)
      .slice()
      .sort((a, b) => {
        // Sort by close time, oldest first — matches the chart's left→right read
        const ta = a.closed_at ? new Date(a.closed_at).getTime() : 0
        const tb = b.closed_at ? new Date(b.closed_at).getTime() : 0
        return ta - tb
      })

    let cumulative = 0
    const chartData = valid.map((e, i) => {
      cumulative += e.pnl_amount
      return {
        // X axis is the trade sequence index, labelled with ticker + date
        idx: i + 1,
        label: `#${i + 1} ${e.ticker}`,
        dateLabel: fmtDate(e.closed_at),
        ticker: e.ticker,
        pnl: e.pnl_amount,
        cumulative,
      }
    })

    const winners = valid.filter((e) => e.pnl_amount > 0)
    const losers = valid.filter((e) => e.pnl_amount < 0)
    const total = valid.reduce((sum, e) => sum + e.pnl_amount, 0)
    const winRate = valid.length > 0 ? (winners.length / valid.length) * 100 : 0
    const avgWin = winners.length > 0
      ? winners.reduce((s, e) => s + e.pnl_amount, 0) / winners.length
      : 0
    const avgLoss = losers.length > 0
      ? losers.reduce((s, e) => s + e.pnl_amount, 0) / losers.length
      : 0
    const biggestWin = winners.length > 0
      ? Math.max(...winners.map((e) => e.pnl_amount))
      : 0
    const biggestLoss = losers.length > 0
      ? Math.min(...losers.map((e) => e.pnl_amount))
      : 0

    return {
      chartData,
      stats: {
        total,
        count: valid.length,
        winners: winners.length,
        losers: losers.length,
        winRate,
        avgWin,
        avgLoss,
        biggestWin,
        biggestLoss,
      },
    }
  }, [closedEntries])

  const totalColor = stats.total >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div className="bg-gradient-to-br from-gray-900 to-gray-900/60 border border-gray-800 rounded-lg p-5 mb-6">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">
          Trade P&amp;L
        </h2>
        <p className="text-[10px] text-gray-500 mt-0.5">
          Realized gains/losses from closed trades · Complements the
          Dashboard's equity curve (which also includes unrealized P&amp;L)
        </p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <StatCard
          label="Total realized"
          value={fmtSigned(stats.total)}
          color={totalColor}
          small={`${stats.count} closed trade${stats.count === 1 ? '' : 's'}`}
        />
        <StatCard
          label="Win rate"
          value={stats.count > 0 ? `${stats.winRate.toFixed(0)}%` : '—'}
          small={`${stats.winners}W / ${stats.losers}L`}
        />
        <StatCard
          label="Avg win"
          value={fmtSigned(stats.avgWin)}
          color="text-emerald-400"
          small={stats.biggestWin > 0 ? `biggest ${fmtSigned(stats.biggestWin)}` : ''}
        />
        <StatCard
          label="Avg loss"
          value={fmtSigned(stats.avgLoss)}
          color="text-red-400"
          small={stats.biggestLoss < 0 ? `biggest ${fmtSigned(stats.biggestLoss)}` : ''}
        />
      </div>

      {/* Cumulative chart */}
      <div className="h-56">
        {chartData.length === 0 ? (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm text-center">
            <div>
              <p>No closed trades yet.</p>
              <p className="text-xs text-gray-600 mt-1">
                Each closed trade in the Journal becomes one point on this chart.
                Cumulative P&amp;L is plotted over time — positive trades push
                the line up, losses pull it down.
              </p>
            </div>
          </div>
        ) : chartData.length === 1 ? (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm text-center">
            <div>
              <p>One closed trade so far — need at least 2 for a line.</p>
              <p className="text-xs text-gray-600 mt-1">
                {chartData[0].ticker}: {fmtSigned(chartData[0].pnl)}
                {' · '}cumulative {fmtSigned(chartData[0].cumulative)}
              </p>
            </div>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis
                dataKey="idx"
                type="number"
                domain={[1, 'dataMax']}
                allowDecimals={false}
                stroke="#6b7280"
                fontSize={10}
                label={{
                  value: 'trade sequence',
                  position: 'insideBottom',
                  offset: -10,
                  fontSize: 10,
                  fill: '#6b7280',
                }}
              />
              <YAxis
                tickFormatter={(v) => fmtSigned(v)}
                stroke="#6b7280"
                fontSize={10}
                width={70}
              />
              <Tooltip
                contentStyle={{
                  background: '#0b1220',
                  border: '1px solid #1f2937',
                  borderRadius: '4px',
                  fontSize: '12px',
                }}
                labelFormatter={(idx, payload) => {
                  const p = payload?.[0]?.payload
                  if (!p) return `Trade #${idx}`
                  return `Trade #${idx} · ${p.ticker} · ${p.dateLabel}`
                }}
                formatter={(value, name, { payload }) => {
                  if (name === 'cumulative') return [fmtSigned(value), 'Cumulative']
                  return [fmtSigned(payload.pnl), 'This trade']
                }}
              />
              {/* Zero line so wins/losses are visually obvious */}
              <ReferenceLine y={0} stroke="#4b5563" strokeDasharray="2 4" />
              <Line
                type="monotone"
                dataKey="cumulative"
                stroke="#818cf8"
                strokeWidth={2}
                dot={<PnlDot />}
                activeDot={{ r: 5 }}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
