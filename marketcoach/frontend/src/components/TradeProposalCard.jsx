import { useState } from 'react'
import ConfirmTradeModal from './ConfirmTradeModal'

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

const SIDE_STYLES = {
  buy: {
    border: 'border-emerald-700/60',
    bg: 'bg-emerald-950/30',
    label: 'BUY',
    labelBg: 'bg-emerald-700',
  },
  sell: {
    border: 'border-red-700/60',
    bg: 'bg-red-950/30',
    label: 'SELL',
    labelBg: 'bg-red-700',
  },
}

export default function TradeProposalCard({
  proposal,
  isLive,
  advisorSessionId,
  onExecuted,
}) {
  const [modalOpen, setModalOpen] = useState(false)
  const [executed, setExecuted] = useState(null) // { status, fill_price, error } once submitted

  const style = SIDE_STYLES[proposal.side] || SIDE_STYLES.buy

  return (
    <>
      <div
        className={`my-3 rounded-lg border ${style.border} ${style.bg} p-4`}
      >
        <div className="flex items-center justify-between gap-3 mb-3">
          <div className="flex items-center gap-2">
            <span
              className={`text-xs font-bold px-2 py-0.5 rounded text-white ${style.labelBg}`}
            >
              {style.label}
            </span>
            <span className="text-lg font-bold text-white">
              {proposal.ticker}
            </span>
            <span className="text-sm text-gray-300">
              {proposal.qty} {proposal.qty === 1 ? 'share' : 'shares'}
            </span>
          </div>
          <span className="text-xs text-gray-500 uppercase">
            {proposal.order_type}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs mb-3">
          {proposal.order_type === 'limit' && (
            <div className="flex justify-between">
              <span className="text-gray-500">Limit</span>
              <span className="text-white font-mono">
                {fmtMoney(proposal.limit_price)}
              </span>
            </div>
          )}
          {proposal.stop_loss !== undefined && proposal.stop_loss !== null && (
            <div className="flex justify-between">
              <span className="text-gray-500">Stop loss</span>
              <span className="text-red-400 font-mono">
                {fmtMoney(proposal.stop_loss)}
              </span>
            </div>
          )}
          {proposal.target_1 !== undefined && proposal.target_1 !== null && (
            <div className="flex justify-between">
              <span className="text-gray-500">Target 1</span>
              <span className="text-emerald-400 font-mono">
                {fmtMoney(proposal.target_1)}
              </span>
            </div>
          )}
          {proposal.target_2 !== undefined && proposal.target_2 !== null && (
            <div className="flex justify-between">
              <span className="text-gray-500">Target 2</span>
              <span className="text-emerald-400 font-mono">
                {fmtMoney(proposal.target_2)}
              </span>
            </div>
          )}
        </div>

        {proposal.rationale && (
          <p className="text-xs text-gray-400 italic mb-3 border-l-2 border-gray-700 pl-2">
            "{proposal.rationale}"
          </p>
        )}

        {executed ? (
          <div
            className={`text-xs px-3 py-2 rounded ${
              executed.status === 'filled' || executed.status === 'accepted'
                ? 'bg-emerald-900/40 text-emerald-200'
                : 'bg-red-900/40 text-red-200'
            }`}
          >
            <div className="font-semibold uppercase tracking-wide mb-0.5">
              {executed.status}
            </div>
            {executed.fill_price && (
              <div>Filled at {fmtMoney(executed.fill_price)}</div>
            )}
            {executed.rejection_reason && (
              <div className="opacity-90">{executed.rejection_reason}</div>
            )}
          </div>
        ) : (
          <button
            onClick={() => setModalOpen(true)}
            className="w-full px-4 py-2 text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white rounded transition"
          >
            Review &amp; execute →
          </button>
        )}

        <p className="text-[10px] text-gray-600 mt-2 text-center">
          Stop loss and targets are descriptive — only the entry order is placed.
          Manage exits manually.
        </p>
      </div>

      {modalOpen && (
        <ConfirmTradeModal
          proposal={proposal}
          isLive={isLive}
          advisorSessionId={advisorSessionId}
          onClose={() => setModalOpen(false)}
          onExecuted={(result) => {
            setExecuted(result)
            setModalOpen(false)
            if (onExecuted) onExecuted(result)
          }}
        />
      )}
    </>
  )
}
