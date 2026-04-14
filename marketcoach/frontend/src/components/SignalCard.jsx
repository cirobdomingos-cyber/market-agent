// Phase 4: full implementation

const SENTIMENT_COLOURS = {
  bullish: 'text-green-400 bg-green-400/10',
  bearish: 'text-red-400 bg-red-400/10',
  neutral: 'text-gray-400 bg-gray-400/10',
}

export default function SignalCard({ signal }) {
  const colourClass = SENTIMENT_COLOURS[signal.sentiment] ?? SENTIMENT_COLOURS.neutral

  return (
    <div className="border border-gray-800 rounded-lg p-3 space-y-1">
      <div className="flex items-center justify-between">
        <span className="font-mono font-bold text-sm">{signal.ticker}</span>
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${colourClass}`}>
          {signal.sentiment}
        </span>
      </div>
      <p className="text-xs text-gray-300 line-clamp-2">{signal.headline}</p>
      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>{signal.source}</span>
        <span>{Math.round(signal.confidence * 100)}% conf</span>
      </div>
    </div>
  )
}
