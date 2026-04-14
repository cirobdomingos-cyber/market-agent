import { useState } from 'react'
import axios from 'axios'

const API = '/api'

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

export default function ConfirmTradeModal({
  proposal,
  isLive,
  advisorSessionId,
  onClose,
  onExecuted,
}) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  // Live-mode safety: a separate checkbox the user must tick before the
  // submit button enables. Backend also validates this — defence in depth.
  const [confirmLive, setConfirmLive] = useState(false)
  // Trade journal — the user MUST type their own thesis before executing.
  // Disagreement is optional. Both feed into the journal entry on success.
  const [userThesis, setUserThesis] = useState('')
  const [userDisagreement, setUserDisagreement] = useState('')

  const thesisValid = userThesis.trim().length >= 10

  const submit = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const body = {
        ticker: proposal.ticker,
        side: proposal.side,
        qty: proposal.qty,
        order_type: proposal.order_type,
        limit_price: proposal.order_type === 'limit' ? proposal.limit_price : null,
        rationale: proposal.rationale || null,
        advisor_session_id: advisorSessionId || null,
        confirm_live_capital: isLive ? confirmLive : false,
        user_thesis: userThesis.trim(),
        user_disagreement: userDisagreement.trim() || null,
      }
      const res = await axios.post(`${API}/orders/confirm`, body)
      onExecuted(res.data)
    } catch (err) {
      const detail = err.response?.data?.detail
      setError(
        Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
          : detail || err.message || 'Order submission failed'
      )
    } finally {
      setSubmitting(false)
    }
  }

  const submitDisabled =
    submitting || !thesisValid || (isLive && !confirmLive)

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose()
      }}
    >
      <div className="bg-gray-900 border border-gray-700 rounded-lg max-w-md w-full p-5 shadow-2xl">
        <h2 className="text-lg font-bold text-white mb-1">Confirm trade</h2>
        <p className="text-xs text-gray-500 mb-4">
          This will submit a real order to your{' '}
          <strong className={isLive ? 'text-red-400' : 'text-emerald-400'}>
            {isLive ? 'LIVE' : 'paper'}
          </strong>{' '}
          Alpaca account.
        </p>

        <div className="space-y-2 text-sm bg-gray-950 rounded p-3 mb-4 border border-gray-800">
          <div className="flex justify-between">
            <span className="text-gray-500">Action</span>
            <span
              className={`font-bold ${
                proposal.side === 'buy' ? 'text-emerald-400' : 'text-red-400'
              }`}
            >
              {proposal.side.toUpperCase()} {proposal.qty}{' '}
              {proposal.qty === 1 ? 'share' : 'shares'} of {proposal.ticker}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Order type</span>
            <span className="text-white">{proposal.order_type}</span>
          </div>
          {proposal.order_type === 'limit' && (
            <div className="flex justify-between">
              <span className="text-gray-500">Limit price</span>
              <span className="text-white font-mono">
                {fmtMoney(proposal.limit_price)}
              </span>
            </div>
          )}
          {proposal.limit_price && (
            <div className="flex justify-between border-t border-gray-800 pt-2 mt-2">
              <span className="text-gray-500">Estimated cost</span>
              <span className="text-white font-mono">
                {fmtMoney(proposal.limit_price * proposal.qty)}
              </span>
            </div>
          )}
        </div>

        {(proposal.stop_loss || proposal.target_1 || proposal.target_2) && (
          <div className="text-xs text-gray-400 mb-4">
            <p className="font-semibold text-gray-300 mb-1">
              Manual exits to set after fill:
            </p>
            <ul className="space-y-0.5 list-disc list-inside">
              {proposal.stop_loss && (
                <li>
                  Stop loss: <span className="text-red-400 font-mono">{fmtMoney(proposal.stop_loss)}</span>
                </li>
              )}
              {proposal.target_1 && (
                <li>
                  Target 1: <span className="text-emerald-400 font-mono">{fmtMoney(proposal.target_1)}</span>
                </li>
              )}
              {proposal.target_2 && (
                <li>
                  Target 2: <span className="text-emerald-400 font-mono">{fmtMoney(proposal.target_2)}</span>
                </li>
              )}
            </ul>
          </div>
        )}

        {proposal.rationale && (
          <div className="mb-4">
            <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
              Advisor's rationale
            </p>
            <p className="text-xs text-gray-400 italic border-l-2 border-gray-700 pl-2">
              "{proposal.rationale}"
            </p>
          </div>
        )}

        {/* Trade journal — required. The whole point of the journal is the
            discipline; if this were optional it'd always be skipped. */}
        <div className="mb-4 p-3 bg-indigo-950/20 border border-indigo-900/50 rounded">
          <label className="block text-xs font-semibold text-indigo-200 mb-1">
            Your thesis <span className="text-red-400">*</span>
          </label>
          <p className="text-[10px] text-gray-500 mb-1.5">
            In your own words. Why are you taking this trade? (10+ chars, required)
          </p>
          <textarea
            value={userThesis}
            onChange={(e) => setUserThesis(e.target.value)}
            placeholder="e.g. NVDA breakout looks clean, volume confirms, AI demand thesis intact"
            rows={2}
            disabled={submitting}
            className="w-full bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500 disabled:opacity-50"
          />
          {userThesis.length > 0 && !thesisValid && (
            <p className="text-[10px] text-amber-400 mt-1">
              Need at least 10 characters
            </p>
          )}

          <label className="block text-xs font-semibold text-indigo-200 mt-3 mb-1">
            Disagreement with the advisor (optional)
          </label>
          <p className="text-[10px] text-gray-500 mb-1.5">
            Where do you see this differently? Leave blank if you fully agree.
          </p>
          <textarea
            value={userDisagreement}
            onChange={(e) => setUserDisagreement(e.target.value)}
            placeholder="e.g. advisor is too cautious on size — I'm taking 2x"
            rows={2}
            disabled={submitting}
            className="w-full bg-gray-950 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500 disabled:opacity-50"
          />
        </div>

        {isLive && (
          <label className="flex items-start gap-2 text-xs text-red-200 mb-4 p-2 bg-red-950/40 border border-red-800 rounded cursor-pointer">
            <input
              type="checkbox"
              checked={confirmLive}
              onChange={(e) => setConfirmLive(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              I understand this places a <strong>real-money order</strong> using my
              live Alpaca account. I have verified the ticker, side, quantity, and
              price above.
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
            className="flex-1 px-4 py-2 text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded transition"
          >
            {submitting ? 'Submitting…' : 'Execute'}
          </button>
        </div>
      </div>
    </div>
  )
}
