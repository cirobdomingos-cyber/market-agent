import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'

const API = '/api'

const DIRECTION_COLORS = {
  long: 'text-green-400 bg-green-400/10',
  short: 'text-red-400 bg-red-400/10',
}

const HORIZON_LABELS = {
  short: '1-5 days',
  medium: '1-4 weeks',
  long: '1-3 months',
}

/**
 * Build a focused prompt asking the Advisor to re-validate a trade idea
 * against current market data.
 *
 * Why we build this prompt here instead of just passing the ticker:
 *   - The idea has stale numbers (entry, stop, target) from whenever the
 *     pipeline generated it. The advisor needs to know those numbers so
 *     it can check whether the setup is still intact against current price.
 *   - The rationale is short context for the advisor — NOT a substitute
 *     for the user's own thesis at execution time. The modal still forces
 *     the user to type their own thesis before the trade goes through.
 *   - By making this a text prompt instead of a structured payload, we
 *     reuse the Advisor's normal message flow with zero backend changes.
 */
function buildAdvisorPrompt(idea) {
  const horizonLabel = HORIZON_LABELS[idea.horizon] || idea.horizon
  const conf = Math.round(idea.confidence * 100)
  return (
    `Review this trade idea from the pipeline and tell me if it's still actionable now.\n\n` +
    `**${idea.direction.toUpperCase()} ${idea.ticker}** — ${horizonLabel}, ${conf}% confidence\n` +
    `- Entry: $${idea.entry_price}\n` +
    `- Stop loss: $${idea.stop_loss}\n` +
    `- Take profit: $${idea.take_profit}\n` +
    `- Position size: ${(idea.position_size_pct * 100).toFixed(1)}% of portfolio\n` +
    `- Original rationale: "${idea.rationale}"\n\n` +
    `Check current price via market_data, verify the thesis is still intact, ` +
    `and if yes, give me a specific executable trade I can click. If the setup ` +
    `has decayed (price moved past entry, thesis broken, etc.), say so and ` +
    `tell me what's different now.`
  )
}

export default function TradeIdeas() {
  const [ideas, setIdeas] = useState([])
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  const askAdvisor = (idea) => {
    const prompt = buildAdvisorPrompt(idea)
    // Use URL-encoded query param so the prompt survives the navigation.
    // Advisor.jsx reads ?prompt= on mount and auto-sends it.
    navigate(`/advisor?prompt=${encodeURIComponent(prompt)}`)
  }

  const fetchIdeas = async () => {
    try {
      const res = await axios.get(`${API}/trade-ideas`)
      setIdeas(res.data)
    } catch (err) {
      console.error('Failed to fetch trade ideas:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchIdeas() }, [])

  const markExecuted = async (id) => {
    try {
      await axios.patch(`${API}/trade-ideas/${id}?status=executed`)
      fetchIdeas()
    } catch (err) {
      console.error('Failed to update idea:', err)
    }
  }

  const markCancelled = async (id) => {
    try {
      await axios.patch(`${API}/trade-ideas/${id}?status=cancelled`)
      fetchIdeas()
    } catch (err) {
      console.error('Failed to update idea:', err)
    }
  }

  if (loading) return <p className="text-gray-500 text-sm">Loading trade ideas...</p>

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Trade Ideas</h1>
        <span className="text-sm text-gray-400">
          {ideas.length} active {ideas.length === 1 ? 'idea' : 'ideas'}
        </span>
      </div>

      {ideas.length === 0 ? (
        <div className="border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-400">No active trade ideas.</p>
          <p className="text-gray-500 text-sm mt-2">
            Run the pipeline from the Dashboard to generate theses and trade ideas.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {ideas.map((idea) => (
            <TradeIdeaCard
              key={idea.id}
              idea={idea}
              onExecute={() => markExecuted(idea.id)}
              onCancel={() => markCancelled(idea.id)}
              onAskAdvisor={() => askAdvisor(idea)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function TradeIdeaCard({ idea, onExecute, onCancel, onAskAdvisor }) {
  const isLong = idea.direction === 'long'
  const colorClass = DIRECTION_COLORS[idea.direction] || DIRECTION_COLORS.long
  const confidencePct = Math.round(idea.confidence * 100)
  const sizePct = (idea.position_size_pct * 100).toFixed(1)
  const maxLossPct = (idea.max_loss_pct * 100).toFixed(2)

  // Calculate potential profit/loss in $ terms (per $10k invested as reference)
  const riskPct = Math.abs((idea.stop_loss - idea.entry_price) / idea.entry_price * 100).toFixed(1)
  const rewardPct = Math.abs((idea.take_profit - idea.entry_price) / idea.entry_price * 100).toFixed(1)

  return (
    <div className="border border-gray-800 rounded-lg p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="font-mono font-bold text-lg">{idea.ticker}</span>
          <span className={`text-xs px-2.5 py-1 rounded-full font-semibold uppercase ${colorClass}`}>
            {idea.direction}
          </span>
          <span className="text-xs text-gray-500 bg-gray-800 px-2 py-1 rounded">
            {HORIZON_LABELS[idea.horizon] || idea.horizon}
          </span>
          <span className="text-xs text-gray-500">
            {confidencePct}% confidence
          </span>
        </div>
        <div className="flex gap-2">
          {/*
            Primary action: hand this idea to the Advisor for fresh validation.
            Deliberately NOT a direct "execute this now" button — the idea's
            numbers can be hours stale, and we want every execution to go
            through the Advisor's interactive checkpoint + required thesis
            capture. This button just pre-fills the prompt and routes you
            through the normal flow.
          */}
          <button
            onClick={onAskAdvisor}
            className="px-3 py-1.5 text-xs bg-indigo-600 hover:bg-indigo-500 rounded font-semibold transition-colors"
            title="Ask the Advisor to re-validate this idea against current price and give you an executable trade"
          >
            Ask Advisor →
          </button>
          <button
            onClick={onExecute}
            className="px-3 py-1.5 text-xs bg-gray-700 hover:bg-gray-600 rounded font-medium transition-colors"
            title="Mark this idea as manually executed (tracking only — does not place an order)"
          >
            Mark Executed
          </button>
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-xs bg-gray-800 hover:bg-gray-700 text-gray-400 rounded font-medium transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>

      {/* Price levels */}
      <div className="grid grid-cols-4 gap-4">
        <PriceBox label="Entry" value={idea.entry_price} />
        <PriceBox
          label="Stop Loss"
          value={idea.stop_loss}
          subtext={`${riskPct}% risk`}
          textColor="text-red-400"
        />
        <PriceBox
          label="Take Profit"
          value={idea.take_profit}
          subtext={`${rewardPct}% reward`}
          textColor="text-green-400"
        />
        <PriceBox
          label="R:R Ratio"
          value={`${idea.risk_reward_ratio}:1`}
          isRatio
        />
      </div>

      {/* Sizing & Risk bar */}
      <div className="grid grid-cols-3 gap-4 text-sm">
        <div>
          <span className="text-gray-500">Position Size:</span>{' '}
          <span className="text-white font-medium">{sizePct}%</span>
          <span className="text-gray-600"> of portfolio</span>
        </div>
        <div>
          <span className="text-gray-500">Max Loss:</span>{' '}
          <span className="text-red-400 font-medium">{maxLossPct}%</span>
          <span className="text-gray-600"> of portfolio</span>
        </div>
        <div>
          <span className="text-gray-500">Strategy:</span>{' '}
          <span className="text-white font-medium">{idea.strategy.replace('_', ' ')}</span>
        </div>
      </div>

      {/* Visual stop/entry/target bar */}
      <StopTargetBar idea={idea} />

      {/* Rationale */}
      <p className="text-sm text-gray-300 leading-relaxed">{idea.rationale}</p>

      {/* Technicals */}
      {idea.key_levels && Object.keys(idea.key_levels).length > 0 && (
        <div className="flex gap-4 text-xs text-gray-500">
          {idea.key_levels.sma_50 && <span>SMA50: ${idea.key_levels.sma_50.toFixed(2)}</span>}
          {idea.key_levels.sma_200 && <span>SMA200: ${idea.key_levels.sma_200.toFixed(2)}</span>}
          {idea.key_levels.rsi_14 && <span>RSI: {idea.key_levels.rsi_14.toFixed(1)}</span>}
        </div>
      )}

      {/* Expiry */}
      <div className="text-xs text-gray-600">
        Expires: {idea.expires_at ? new Date(idea.expires_at).toLocaleDateString() : 'N/A'}
      </div>
    </div>
  )
}

function PriceBox({ label, value, subtext, textColor = 'text-white', isRatio = false }) {
  return (
    <div className="border border-gray-800 rounded p-3">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-lg font-bold ${textColor} mt-0.5`}>
        {isRatio ? value : `$${Number(value).toFixed(2)}`}
      </p>
      {subtext && <p className={`text-xs ${textColor} opacity-70`}>{subtext}</p>}
    </div>
  )
}

function StopTargetBar({ idea }) {
  // Visual representation of stop loss / entry / take profit
  const prices = [idea.stop_loss, idea.entry_price, idea.take_profit].sort((a, b) => a - b)
  const min = prices[0] * 0.99
  const max = prices[2] * 1.01
  const range = max - min

  const pct = (price) => ((price - min) / range * 100).toFixed(1)

  return (
    <div className="relative h-6 bg-gray-800 rounded-full overflow-hidden">
      {/* Risk zone (entry to stop) */}
      <div
        className="absolute h-full bg-red-500/20"
        style={{
          left: `${pct(Math.min(idea.stop_loss, idea.entry_price))}%`,
          width: `${Math.abs(pct(idea.entry_price) - pct(idea.stop_loss))}%`,
        }}
      />
      {/* Reward zone (entry to target) */}
      <div
        className="absolute h-full bg-green-500/20"
        style={{
          left: `${pct(Math.min(idea.entry_price, idea.take_profit))}%`,
          width: `${Math.abs(pct(idea.take_profit) - pct(idea.entry_price))}%`,
        }}
      />
      {/* Stop loss marker */}
      <div
        className="absolute top-0 h-full w-0.5 bg-red-500"
        style={{ left: `${pct(idea.stop_loss)}%` }}
      />
      {/* Entry marker */}
      <div
        className="absolute top-0 h-full w-1 bg-white"
        style={{ left: `${pct(idea.entry_price)}%` }}
      />
      {/* Target marker */}
      <div
        className="absolute top-0 h-full w-0.5 bg-green-500"
        style={{ left: `${pct(idea.take_profit)}%` }}
      />
    </div>
  )
}
