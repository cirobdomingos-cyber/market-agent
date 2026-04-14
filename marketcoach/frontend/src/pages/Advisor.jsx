import { useState, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import axios from 'axios'
import ReactMarkdown from 'react-markdown'
import ChatWindow from '../components/ChatWindow'
import TradeProposalCard from '../components/TradeProposalCard'
import { extractTradeProposals } from '../lib/extractTradeProposals'

const API = '/api'

// Framework modes from the TradingAdvisor system prompt. Each button sends
// a canonical phrase that the agent's mode-detection recognises.
const MODE_PROMPTS = [
  { label: 'Scan market', prompt: 'Scan the market' },
  { label: 'Weekly plan', prompt: 'Weekly plan' },
  { label: 'Review portfolio', prompt: 'Review portfolio' },
  { label: 'Analyze NVDA', prompt: 'Analyze NVDA' },
]

function formatMoney(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

function formatPct(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

// localStorage key for the persistent sessionId. Until the user explicitly
// clicks "New chat", every navigation back to the Advisor page reuses the
// same session so the conversation history survives refreshes and page
// transitions.
const SESSION_STORAGE_KEY = 'marketcoach_advisor_session_id'

function readOrCreateSessionId() {
  try {
    const existing = localStorage.getItem(SESSION_STORAGE_KEY)
    if (existing && /^advisor-[a-zA-Z0-9_-]+$/.test(existing)) return existing
  } catch {
    /* localStorage may be disabled (rare); fall through */
  }
  const fresh = `advisor-${Date.now()}`
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, fresh)
  } catch {
    /* ignore */
  }
  return fresh
}

export default function Advisor() {
  // Persistent sessionId so refreshing or navigating away doesn't wipe the
  // conversation. "New chat" button below rotates it on demand.
  const [sessionId, setSessionId] = useState(readOrCreateSessionId)
  const [messages, setMessages] = useState([])
  const [account, setAccount] = useState(null)
  const [positions, setPositions] = useState([])
  const [error, setError] = useState(null)
  // "Thinking..." placeholder state while an advisor call is in flight.
  // Renders as a pseudo-assistant message below the user's input so there's
  // clear visual feedback that something is happening during the 30-90s wait.
  const [isSending, setIsSending] = useState(false)
  const messagesEndRef = useRef(null)
  // Deep-link support: /advisor?prompt=... auto-sends that prompt on mount.
  // Used by the "Ask Advisor" button on the Trade Ideas page. We track
  // whether we've already fired the auto-send via a ref so React strict
  // mode's double-mount doesn't send it twice.
  const [searchParams, setSearchParams] = useSearchParams()
  const autoSentRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    async function fetchPortfolio() {
      try {
        const res = await axios.get(`${API}/portfolio`)
        if (cancelled) return
        setAccount(res.data.account || null)
        setPositions(res.data.positions || [])
      } catch (err) {
        console.error('Failed to fetch portfolio:', err)
      }
    }
    fetchPortfolio()
    // Auto-refresh every 15s so the sidebar stays live without hitting Refresh
    const id = setInterval(fetchPortfolio, 15_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  // Load the conversation history for this sessionId on mount (and whenever
  // the session rotates via "New chat"). Any prior messages from this session
  // that were persisted to the chat_messages table come back onto the screen.
  useEffect(() => {
    let cancelled = false
    async function loadHistory() {
      try {
        const res = await axios.get(`${API}/chat/${sessionId}`)
        if (cancelled) return
        // Backend returns [{id, role, content, created_at}, ...]. We only
        // need role + content for rendering; the rest is metadata.
        const history = (res.data || []).map((m) => ({
          role: m.role,
          content: m.content,
        }))
        setMessages(history)
      } catch (err) {
        // A brand-new session returns [] or 404; either way, empty state
        // is the right UI.
        if (!cancelled) setMessages([])
      }
    }
    loadHistory()
    return () => {
      cancelled = true
    }
  }, [sessionId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isSending])

  const handleSend = async (message) => {
    setError(null)
    setMessages((prev) => [...prev, { role: 'user', content: message }])
    setIsSending(true)

    try {
      const res = await axios.post(`${API}/advisor`, {
        session_id: sessionId,
        message,
      })
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: res.data.response },
      ])
    } catch (err) {
      const detail = err.response?.data?.detail || 'Advisor call failed.'
      setError(detail)
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `**Error:** ${detail}`,
        },
      ])
    } finally {
      setIsSending(false)
    }
  }

  const startNewChat = () => {
    const fresh = `advisor-${Date.now()}`
    try {
      localStorage.setItem(SESSION_STORAGE_KEY, fresh)
    } catch {
      /* ignore */
    }
    setSessionId(fresh)  // triggers loadHistory which returns []
    setMessages([])
    setError(null)
    autoSentRef.current = false  // so a fresh ?prompt= on the new session works
  }

  // Auto-send the prompt from ?prompt=... on mount (deep-link from Trade
  // Ideas "Ask Advisor" button). Runs exactly once per navigation.
  useEffect(() => {
    if (autoSentRef.current) return
    const prompt = searchParams.get('prompt')
    if (!prompt) return
    autoSentRef.current = true
    // Clear the query param so refreshing the page doesn't re-trigger
    const next = new URLSearchParams(searchParams)
    next.delete('prompt')
    setSearchParams(next, { replace: true })
    // Fire the send — handleSend is stable within this render tree
    handleSend(prompt)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const alpacaConnected =
    account && account.status !== 'disconnected' && !account.error

  return (
    <div className="flex flex-col lg:flex-row gap-4 lg:gap-6 lg:h-[85vh]">
      {/* Chat column */}
      <div className="flex-1 flex flex-col border border-gray-800 rounded-lg min-h-[60vh] lg:min-h-0">
        <div className="border-b border-gray-800 px-4 py-3 flex items-start justify-between gap-3">
          <div className="flex-1">
            <h1 className="text-lg font-semibold text-white">Trading Advisor</h1>
            <p className="text-xs text-gray-500 mt-0.5">
              Decisive trade ideas with stop loss, targets, and risk/reward.
              Uses live broker data + market tools. Not financial advice —
              this is your personal research.
            </p>
          </div>
          <button
            onClick={startNewChat}
            title="Clear this conversation and start fresh"
            className="shrink-0 px-2.5 py-1 text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded text-gray-300 transition-colors"
          >
            New chat
          </button>
        </div>

        <div className="flex-1 p-4 overflow-y-auto space-y-4">
          {messages.length === 0 && (
            <div className="text-gray-500 text-sm space-y-3">
              <p>Pick a mode or type your own question.</p>
              <div className="flex flex-wrap gap-2">
                {MODE_PROMPTS.map((m) => (
                  <button
                    key={m.label}
                    onClick={() => handleSend(m.prompt)}
                    className="px-3 py-1.5 text-xs bg-indigo-900/40 hover:bg-indigo-800/60
                               border border-indigo-700/50 rounded-full text-indigo-200
                               transition-colors"
                  >
                    {m.label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-gray-600 pt-2">
                Heads up: advisor calls can take 30–90 seconds — it runs macro,
                sector, and per-ticker analysis with live tool calls.
              </p>
            </div>
          )}

          {messages.map((m, i) => {
            // For assistant messages, extract any structured trade proposals
            // and strip them from the markdown so they don't render twice.
            const isAssistant = m.role === 'assistant'
            const { proposals, cleanedMarkdown } = isAssistant
              ? extractTradeProposals(m.content)
              : { proposals: [], cleanedMarkdown: m.content }
            return (
              <div
                key={i}
                className={`text-sm ${
                  m.role === 'user' ? 'text-gray-300' : 'text-indigo-100'
                }`}
              >
                <span className="text-xs text-gray-500 uppercase font-medium">
                  {m.role === 'user' ? 'You' : 'Advisor'}
                </span>
                <div className="mt-1 prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown>{cleanedMarkdown}</ReactMarkdown>
                </div>
                {proposals.map((p, idx) => (
                  <TradeProposalCard
                    key={`${i}-${idx}`}
                    proposal={p}
                    isLive={!account?.paper}
                    advisorSessionId={sessionId}
                    onExecuted={() => {
                      // Refresh portfolio sidebar after a successful execution
                      axios
                        .get(`${API}/portfolio`)
                        .then((res) => {
                          setAccount(res.data.account || null)
                          setPositions(res.data.positions || [])
                        })
                        .catch(() => {})
                    }}
                  />
                ))}
              </div>
            )
          })}

          {/* Thinking indicator while an advisor call is in flight. Appears
              below the user's most recent message so the wait state is
              obvious — advisor calls take 30–90s for tool-heavy analysis. */}
          {isSending && (
            <div className="text-sm text-indigo-100">
              <span className="text-xs text-gray-500 uppercase font-medium">
                Advisor
              </span>
              <div className="mt-1 flex items-center gap-2 text-indigo-200 italic">
                <span className="inline-flex gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
                  <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" style={{ animationDelay: '300ms' }} />
                </span>
                <span>Thinking… (may take 30–90s for macro + tool analysis)</span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="border-t border-gray-800 p-4">
          <ChatWindow
            sessionId={sessionId}
            onSend={handleSend}
            placeholder="Scan the market, analyze TSLA, review portfolio…"
            loadingPlaceholder="Analyzing…"
          />
        </div>
      </div>

      {/* Sidebar — stacks below chat on mobile, fixed-width on desktop */}
      <div className="w-full lg:w-72 space-y-4 text-sm">
        <div>
          <p className="font-semibold text-gray-300 mb-2">Account</p>
          {alpacaConnected ? (
            <div className="space-y-1 text-gray-400">
              <div className="flex justify-between">
                <span>Portfolio value</span>
                <span className="text-white">
                  {formatMoney(account.portfolio_value)}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Buying power</span>
                <span className="text-white">
                  {formatMoney(account.buying_power)}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Cash</span>
                <span className="text-white">{formatMoney(account.cash)}</span>
              </div>
              <div className="flex justify-between text-xs pt-1">
                <span>Mode</span>
                <span className="text-emerald-400">
                  {account.paper ? 'paper' : 'live'}
                </span>
              </div>
            </div>
          ) : (
            <div className="text-xs text-amber-400">
              Alpaca not connected. Set ALPACA_API_KEY and ALPACA_SECRET_KEY to
              enable live sizing.
            </div>
          )}
        </div>

        <div>
          <p className="font-semibold text-gray-300 mb-2">
            Positions ({positions.length})
          </p>
          {positions.length === 0 ? (
            <p className="text-xs text-gray-500">No open positions.</p>
          ) : (
            <div className="space-y-1.5">
              {positions.map((p) => (
                <div
                  key={p.ticker}
                  className="text-xs flex justify-between border-b border-gray-800/50 pb-1"
                >
                  <span className="text-gray-300 font-medium">{p.ticker}</span>
                  <span
                    className={
                      (p.unrealised_pnl_pct ?? 0) >= 0
                        ? 'text-emerald-400'
                        : 'text-red-400'
                    }
                  >
                    {formatPct(p.unrealised_pnl_pct)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="pt-2 border-t border-gray-800">
          <p className="text-xs text-gray-500 leading-relaxed">
            <strong className="text-gray-400">Hard rules:</strong> 2% max risk
            per trade. 20% max per position. Never below 2:1 R/R. The advisor
            proposes; you click <strong className="text-gray-400">Execute</strong> to
            place the order.
          </p>
        </div>

        {error && (
          <div className="p-2 bg-red-900/30 border border-red-800 text-red-200 rounded text-xs">
            {error}
          </div>
        )}
      </div>
    </div>
  )
}
