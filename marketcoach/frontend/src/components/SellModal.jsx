import { useState, useMemo } from 'react'
import axios from 'axios'

const API = '/api'

/**
 * SellModal — direct-sell UI for an existing position.
 *
 * Exists because going through the Advisor chat for a trim/close is
 * overkill when you already know:
 *   - what you own (visible on the Portfolio page)
 *   - what you want to do (sell some or all)
 *   - at what price (your own plan, possibly from an earlier advisor chat)
 *
 * The advisor's value is in validating NEW entries against live data.
 * For closing an EXISTING position, the math is simpler and the
 * decision is already made — just execute. This modal still captures
 * a user thesis and routes through /orders/confirm's six safety gates.
 *
 * Preset buttons (25 / 50 / 75 / 100) map common partial-close sizes
 * to one-click actions. Manual input overrides for any other qty.
 */

const QUANTITY_PRESETS = [
  { label: '25%', pct: 0.25 },
  { label: '50%', pct: 0.5 },
  { label: '75%', pct: 0.75 },
  { label: '100% (Close)', pct: 1.0 },
]

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtPct(v) {
  if (v === null || v === undefined) return ''
  const n = Number(v)
  if (Number.isNaN(n)) return ''
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

export default function SellModal({ position, isLive, onClose, onExecuted }) {
  const currentQty = Math.floor(Number(position.qty) || 0)
  const avgEntry = Number(position.avg_entry) || 0
  const currentPrice = position.current_price !== null ? Number(position.current_price) : null

  const [qty, setQty] = useState(Math.floor(currentQty * 0.25))  // default 25% trim
  const [orderType, setOrderType] = useState('limit')             // default limit is safer
  const [limitPrice, setLimitPrice] = useState(
    currentPrice ? currentPrice.toFixed(2) : ''
  )
  const [thesis, setThesis] = useState('')
  const [confirmLive, setConfirmLive] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  // Estimated P&L on the sold portion for the summary card
  const estimatedPnl = useMemo(() => {
    const sellPrice = orderType === 'limit'
      ? parseFloat(limitPrice) || 0
      : (currentPrice || 0)
    if (!sellPrice || !avgEntry || !qty) return null
    return (sellPrice - avgEntry) * qty
  }, [qty, orderType, limitPrice, avgEntry, currentPrice])

  const estimatedPnlPct = useMemo(() => {
    const sellPrice = orderType === 'limit'
      ? parseFloat(limitPrice) || 0
      : (currentPrice || 0)
    if (!sellPrice || !avgEntry) return null
    return ((sellPrice - avgEntry) / avgEntry) * 100
  }, [orderType, limitPrice, avgEntry, currentPrice])

  const setPresetQty = (pct) => {
    setQty(Math.floor(currentQty * pct))
  }

  const thesisValid = thesis.trim().length >= 10
  const qtyValid = qty > 0 && qty <= currentQty
  const priceValid =
    orderType === 'market' ||
    (orderType === 'limit' && parseFloat(limitPrice) > 0)
  const submitDisabled =
    submitting ||
    !thesisValid ||
    !qtyValid ||
    !priceValid ||
    (isLive && !confirmLive)

  const submit = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const body = {
        ticker: position.ticker,
        side: 'sell',
        qty: Number(qty),
        order_type: orderType,
        limit_price: orderType === 'limit' ? parseFloat(limitPrice) : null,
        rationale: null,             // not advisor-originated
        advisor_session_id: null,    // not advisor-originated
        confirm_live_capital: isLive ? confirmLive : false,
        user_thesis: thesis.trim(),
        user_disagreement: null,
      }
      const res = await axios.post(`${API}/orders/confirm`, body)
      onExecuted(res.data)
    } catch (err) {
      const detail = err.response?.data?.detail
      setError(
        Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
          : detail || err.message || 'Sell submission failed'
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose()
      }}
    >
      <div className="bg-gray-900 border border-gray-700 rounded-lg max-w-md w-full p-5 shadow-2xl">
        <div className="flex items-start justify-between gap-3 mb-1">
          <h2 className="text-lg font-bold text-white">
            Sell {position.ticker}
          </h2>
          <span
            className={`text-xs px-2 py-0.5 rounded ${
              isLive
                ? 'bg-red-900/50 border border-red-700 text-red-200 font-bold'
                : 'bg-emerald-900/40 border border-emerald-700 text-emerald-200'
            }`}
          >
            {isLive ? 'LIVE' : 'paper'}
          </span>
        </div>
        <p className="text-xs text-gray-500 mb-4">
          Sell some or all of your {position.ticker} position. Bypasses the
          advisor chat for speed — same safety gates and thesis discipline
          as the Execute button.
        </p>

        {/* Position summary */}
        <div className="space-y-1 text-sm bg-gray-950 rounded p-3 mb-4 border border-gray-800">
          <div className="flex justify-between">
            <span className="text-gray-500">Holding</span>
            <span className="text-white font-mono">
              {currentQty} shares @ {fmtMoney(avgEntry)}
            </span>
          </div>
          {currentPrice !== null && (
            <div className="flex justify-between">
              <span className="text-gray-500">Current price</span>
              <span className="text-white font-mono">{fmtMoney(currentPrice)}</span>
            </div>
          )}
          {currentPrice !== null && avgEntry > 0 && (
            <div className="flex justify-between">
              <span className="text-gray-500">Unrealized</span>
              <span
                className={
                  (currentPrice - avgEntry) >= 0
                    ? 'text-emerald-400 font-mono'
                    : 'text-red-400 font-mono'
                }
              >
                {fmtMoney((currentPrice - avgEntry) * currentQty)}{' '}
                ({fmtPct(((currentPrice - avgEntry) / avgEntry) * 100)})
              </span>
            </div>
          )}
        </div>

        {/* Quantity — presets + manual */}
        <label className="block text-xs font-semibold text-gray-300 mb-1">
          Quantity to sell <span className="text-red-400">*</span>
        </label>
        <div className="flex gap-1.5 mb-2">
          {QUANTITY_PRESETS.map((p) => {
            const presetQty = Math.floor(currentQty * p.pct)
            const selected = qty === presetQty
            return (
              <button
                key={p.label}
                type="button"
                onClick={() => setPresetQty(p.pct)}
                className={`flex-1 px-2 py-1.5 text-xs rounded transition-colors ${
                  selected
                    ? 'bg-indigo-600 text-white font-semibold'
                    : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {p.label}
              </button>
            )
          })}
        </div>
        <div className="flex items-center gap-2 mb-4">
          <input
            type="number"
            min={1}
            max={currentQty}
            step={1}
            value={qty}
            onChange={(e) => setQty(parseInt(e.target.value, 10) || 0)}
            disabled={submitting}
            className="flex-1 bg-gray-950 border border-gray-700 rounded px-3 py-1.5 text-sm font-mono text-white focus:outline-none focus:border-indigo-500 disabled:opacity-50"
          />
          <span className="text-xs text-gray-500">of {currentQty}</span>
        </div>
        {!qtyValid && (
          <p className="text-[11px] text-amber-400 -mt-3 mb-3">
            Quantity must be between 1 and {currentQty}
          </p>
        )}

        {/* Order type */}
        <label className="block text-xs font-semibold text-gray-300 mb-1">
          Order type
        </label>
        <div className="flex gap-2 mb-3">
          {['limit', 'market'].map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setOrderType(t)}
              className={`flex-1 px-3 py-1.5 text-xs rounded transition-colors ${
                orderType === t
                  ? 'bg-indigo-600 text-white font-semibold'
                  : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
              }`}
            >
              {t}
            </button>
          ))}
        </div>

        {/* Limit price (only when order type is limit) */}
        {orderType === 'limit' && (
          <>
            <label className="block text-xs font-semibold text-gray-300 mb-1">
              Limit price <span className="text-red-400">*</span>
            </label>
            <input
              type="number"
              min={0.01}
              step={0.01}
              value={limitPrice}
              onChange={(e) => setLimitPrice(e.target.value)}
              disabled={submitting}
              placeholder="e.g. 458.00"
              className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-1.5 text-sm font-mono text-white focus:outline-none focus:border-indigo-500 disabled:opacity-50 mb-3"
            />
          </>
        )}

        {/* Estimated P&L on the trimmed portion */}
        {estimatedPnl !== null && (
          <div className="bg-gray-950 border border-gray-800 rounded p-2 mb-4 text-xs">
            <div className="flex justify-between text-gray-400">
              <span>Estimated realized on this sale</span>
              <span
                className={
                  estimatedPnl >= 0
                    ? 'text-emerald-400 font-mono'
                    : 'text-red-400 font-mono'
                }
              >
                {estimatedPnl >= 0 ? '+' : ''}
                {fmtMoney(estimatedPnl)} ({fmtPct(estimatedPnlPct)})
              </span>
            </div>
            {qty < currentQty && (
              <div className="flex justify-between text-gray-500 mt-0.5">
                <span>Remaining after sale</span>
                <span className="font-mono">
                  {currentQty - qty} shares
                </span>
              </div>
            )}
          </div>
        )}

        {/* Thesis — required */}
        <label className="block text-xs font-semibold text-indigo-200 mb-1">
          Your thesis <span className="text-red-400">*</span>
        </label>
        <p className="text-[10px] text-gray-500 mb-1.5">
          Why are you selling? Required — 10+ chars. Saved to the journal.
        </p>
        <textarea
          value={thesis}
          onChange={(e) => setThesis(e.target.value)}
          placeholder="e.g. Trim at resistance per earlier advisor plan — locking in ~$765 gain, letting 170 shares run toward $470+"
          rows={2}
          disabled={submitting}
          className="w-full bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500 disabled:opacity-50 mb-3"
        />
        {thesis.length > 0 && !thesisValid && (
          <p className="text-[11px] text-amber-400 -mt-2 mb-3">
            Need at least 10 characters
          </p>
        )}

        {/* Live mode acknowledgement */}
        {isLive && (
          <label className="flex items-start gap-2 text-xs text-red-200 mb-3 p-2 bg-red-950/40 border border-red-800 rounded cursor-pointer">
            <input
              type="checkbox"
              checked={confirmLive}
              onChange={(e) => setConfirmLive(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              I understand this places a <strong>real-money sell order</strong>{' '}
              on my live account. I've verified the ticker, quantity, and price.
            </span>
          </label>
        )}

        {error && (
          <div className="text-xs text-red-300 bg-red-950/40 border border-red-800 rounded p-2 mb-3">
            {error}
          </div>
        )}

        <div className="flex gap-2">
          <button
            onClick={onClose}
            disabled={submitting}
            className="flex-1 px-4 py-2 text-sm bg-gray-800 hover:bg-gray-700 disabled:opacity-50 text-gray-300 rounded transition"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitDisabled}
            className="flex-1 px-4 py-2 text-sm font-semibold bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded transition"
          >
            {submitting ? 'Submitting…' : `Sell ${qty}`}
          </button>
        </div>
      </div>
    </div>
  )
}
