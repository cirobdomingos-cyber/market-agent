// Phase 4: full implementation

const DIRECTION_COLOURS = {
  bullish: 'bg-green-500',
  bearish: 'bg-red-500',
  neutral: 'bg-gray-500',
}

export default function ThesisPanel({ thesis }) {
  const barColour = DIRECTION_COLOURS[thesis.direction] ?? DIRECTION_COLOURS.neutral
  const confidencePct = Math.round(thesis.confidence * 100)

  return (
    <div className="border border-gray-800 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <span className="font-mono font-bold">{thesis.ticker}</span>
        <span className="text-xs text-gray-400">{thesis.timeframe}</span>
      </div>

      <div className="space-y-1">
        <div className="flex justify-between text-xs text-gray-400">
          <span>{thesis.direction}</span>
          <span>{confidencePct}%</span>
        </div>
        <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full ${barColour}`}
            style={{ width: `${confidencePct}%` }}
          />
        </div>
      </div>

      <p className="text-xs text-gray-300 line-clamp-3">{thesis.reasoning}</p>
    </div>
  )
}
