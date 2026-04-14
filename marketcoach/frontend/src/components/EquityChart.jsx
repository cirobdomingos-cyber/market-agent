import { useState, useEffect, useMemo } from 'react'
import axios from 'axios'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'

const API = '/api'

const RANGES = [
  { id: '1d', label: '1D', days: 1 },
  { id: '1w', label: '1W', days: 7 },
  { id: '1m', label: '1M', days: 30 },
  { id: 'all', label: 'All', days: 365 },
]

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

function fmtPct(v) {
  if (v === null || v === undefined) return ''
  const n = Number(v)
  if (Number.isNaN(n)) return ''
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

function fmtAxisTime(iso, rangeDays) {
  if (!iso) return ''
  const d = new Date(iso)
  if (rangeDays <= 1) {
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function fmtTooltipTime(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

/**
 * Compute Today / This Week / All-time P&L from a chronological list of
 * equity snapshots. All deltas are calculated as equity_now - equity_at_cutoff
 * where cutoff is the first point on/after the window start.
 */
function computeStats(points) {
  if (!points || points.length === 0) {
    return { today: null, week: null, all: null, latest: null }
  }
  const latest = points[points.length - 1].equity

  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfWeek = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)

  // Find the first snapshot >= each cutoff
  const findBase = (cutoff) => {
    const cutoffMs = cutoff.getTime()
    // First entry at or after cutoff — if none exists yet today, fall back
    // to the earliest known point
    for (const p of points) {
      if (new Date(p.timestamp).getTime() >= cutoffMs) return p.equity
    }
    return points[0].equity
  }

  const todayBase = findBase(startOfToday)
  const weekBase = findBase(startOfWeek)
  const allBase = points[0].equity

  return {
    latest,
    today: latest - todayBase,
    week: latest - weekBase,
    all: latest - allBase,
    // Percent versions for the small print under each card
    todayPct: todayBase ? ((latest - todayBase) / todayBase) * 100 : null,
    weekPct: weekBase ? ((latest - weekBase) / weekBase) * 100 : null,
    allPct: allBase ? ((latest - allBase) / allBase) * 100 : null,
  }
}

function StatCard({ label, value, pct, small }) {
  const isPositive = (value ?? 0) >= 0
  const color = value === null ? 'text-gray-500'
    : isPositive ? 'text-emerald-400'
    : 'text-red-400'
  return (
    <div className="border border-gray-800 rounded-lg p-3">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-lg font-bold mt-1 ${color}`}>
        {value === null ? '—' : fmtSigned(value)}
      </p>
      {pct !== null && pct !== undefined && (
        <p className={`text-xs ${color} opacity-70`}>{fmtPct(pct)}</p>
      )}
      {small && <p className="text-[10px] text-gray-600 mt-0.5">{small}</p>}
    </div>
  )
}

export default function EquityChart() {
  const [range, setRange] = useState('1w')
  const [points, setPoints] = useState([])
  const [loading, setLoading] = useState(true)

  const selectedRange = RANGES.find((r) => r.id === range) || RANGES[1]

  const fetchHistory = async (days) => {
    try {
      const res = await axios.get(`${API}/equity-history?days=${days}`)
      setPoints(res.data || [])
    } catch (err) {
      console.error('Failed to fetch equity history:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    fetchHistory(selectedRange.days)
    // Auto-refresh every 60s so the chart catches up as the polling job
    // writes new snapshots. Poll interval is 5 min on the backend, so
    // the new points arrive roughly on schedule.
    const id = setInterval(() => fetchHistory(selectedRange.days), 60_000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range])

  const stats = useMemo(() => computeStats(points), [points])

  // Recharts needs numeric timestamps on the X axis for clean spacing
  const chartData = useMemo(
    () =>
      points.map((p) => ({
        t: new Date(p.timestamp).getTime(),
        iso: p.timestamp,
        equity: p.equity,
      })),
    [points]
  )

  // Baseline for the reference line — first equity value in the selected
  // range. Makes it visually obvious whether you're up or down.
  const baseline = chartData.length > 0 ? chartData[0].equity : null

  return (
    <div className="bg-gradient-to-br from-gray-900 to-gray-900/60 border border-gray-800 rounded-lg p-5">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">
            Equity Curve
          </h2>
          <p className="text-[10px] text-gray-500 mt-0.5">
            Live snapshots every 5 min · Based on broker account equity
          </p>
        </div>
        <div className="flex gap-1">
          {RANGES.map((r) => (
            <button
              key={r.id}
              onClick={() => setRange(r.id)}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${
                range === r.id
                  ? 'bg-indigo-600 text-white font-semibold'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* Stats row — computed from the full point set */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        <StatCard label="Current" value={stats.latest} pct={null} small="equity" />
        <StatCard label="Today" value={stats.today} pct={stats.todayPct} />
        <StatCard label="This Week" value={stats.week} pct={stats.weekPct} />
        <StatCard label="All-time" value={stats.all} pct={stats.allPct} />
      </div>

      {/* Chart */}
      <div className="h-56">
        {loading ? (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm">
            Loading equity history…
          </div>
        ) : chartData.length < 2 ? (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm text-center">
            <div>
              <p>Not enough history yet.</p>
              <p className="text-xs text-gray-600 mt-1">
                The polling job writes a new snapshot every 5 minutes — your
                chart will fill in as time passes.
              </p>
              <p className="text-xs text-gray-600 mt-1">
                {points.length} point{points.length === 1 ? '' : 's'} recorded so far.
              </p>
            </div>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#818cf8" stopOpacity={0.35} />
                  <stop offset="95%" stopColor="#818cf8" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis
                dataKey="t"
                type="number"
                domain={['dataMin', 'dataMax']}
                tickFormatter={(ms) => fmtAxisTime(new Date(ms).toISOString(), selectedRange.days)}
                stroke="#6b7280"
                fontSize={10}
              />
              <YAxis
                domain={['dataMin - 100', 'dataMax + 100']}
                tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
                stroke="#6b7280"
                fontSize={10}
                width={50}
              />
              <Tooltip
                contentStyle={{
                  background: '#0b1220',
                  border: '1px solid #1f2937',
                  borderRadius: '4px',
                  fontSize: '12px',
                }}
                labelFormatter={(ms) => fmtTooltipTime(new Date(ms).toISOString())}
                formatter={(v) => [fmtMoney(v), 'Equity']}
              />
              {baseline !== null && (
                <ReferenceLine
                  y={baseline}
                  stroke="#4b5563"
                  strokeDasharray="2 4"
                  label={{
                    value: `start ${fmtMoney(baseline)}`,
                    fill: '#6b7280',
                    fontSize: 9,
                    position: 'insideTopLeft',
                  }}
                />
              )}
              <Area
                type="monotone"
                dataKey="equity"
                stroke="#818cf8"
                strokeWidth={1.5}
                fill="url(#equityGradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
