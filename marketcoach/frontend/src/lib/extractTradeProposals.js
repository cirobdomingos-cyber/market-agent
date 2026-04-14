/**
 * Extract structured trade proposals from an advisor markdown response.
 *
 * The TradingAdvisor (when enable_trade_proposals=True) emits trades as
 * fenced code blocks tagged `trade-proposal`:
 *
 *     ```trade-proposal
 *     { "ticker": "NVDA", "side": "buy", "qty": 10, ... }
 *     ```
 *
 * This utility:
 *   1. Finds every such block in the markdown
 *   2. Parses the JSON
 *   3. Returns { proposals, cleanedMarkdown } where cleanedMarkdown has
 *      the blocks stripped so they don't render as raw code below the
 *      proper trade card UI.
 *
 * Malformed JSON is logged and silently dropped — better to lose one
 * proposal than to crash the chat. The user can always re-ask.
 */

const TRADE_BLOCK_REGEX = /```trade-proposal\s*\n([\s\S]*?)\n```/g

const REQUIRED_FIELDS = ['ticker', 'side', 'qty', 'order_type']
const VALID_SIDES = new Set(['buy', 'sell'])
const VALID_ORDER_TYPES = new Set(['market', 'limit'])

function isValidProposal(p) {
  if (!p || typeof p !== 'object') return false
  for (const f of REQUIRED_FIELDS) {
    if (p[f] === undefined || p[f] === null) return false
  }
  if (typeof p.ticker !== 'string' || !/^[A-Z]{1,5}$/.test(p.ticker)) return false
  if (!VALID_SIDES.has(p.side)) return false
  if (typeof p.qty !== 'number' || p.qty <= 0) return false
  if (!VALID_ORDER_TYPES.has(p.order_type)) return false
  if (p.order_type === 'limit' && typeof p.limit_price !== 'number') return false
  if (p.order_type === 'market' && p.limit_price !== undefined && p.limit_price !== null) return false
  return true
}

export function extractTradeProposals(markdown) {
  if (!markdown || typeof markdown !== 'string') {
    return { proposals: [], cleanedMarkdown: markdown || '' }
  }

  const proposals = []
  let cleanedMarkdown = markdown
  let match

  // Reset the regex state between calls
  TRADE_BLOCK_REGEX.lastIndex = 0

  while ((match = TRADE_BLOCK_REGEX.exec(markdown)) !== null) {
    const jsonText = match[1].trim()
    try {
      const parsed = JSON.parse(jsonText)
      if (isValidProposal(parsed)) {
        proposals.push(parsed)
      } else {
        console.warn('Trade proposal failed validation:', parsed)
      }
    } catch (err) {
      console.warn('Failed to parse trade-proposal JSON:', err.message, jsonText)
    }
  }

  // Strip the blocks from the markdown so they don't render as code below
  // the structured cards. Replace with a single newline to preserve spacing.
  cleanedMarkdown = markdown.replace(TRADE_BLOCK_REGEX, '').replace(/\n{3,}/g, '\n\n').trim()

  return { proposals, cleanedMarkdown }
}
