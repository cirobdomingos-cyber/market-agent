// Phase 4: full implementation — markdown rendering, thinking indicator, session management
import { useState } from 'react'

export default function ChatWindow({
  sessionId,
  onSend,
  placeholder = 'Ask the coach anything…',
  loadingPlaceholder = 'Thinking…',
}) {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!input.trim() || loading) return

    const message = input.trim()
    setInput('')
    setLoading(true)

    try {
      await onSend(message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex gap-2">
      <input
        className="flex-1 bg-gray-900 border border-gray-700 rounded px-4 py-2 text-sm
                   focus:outline-none focus:border-indigo-500 disabled:opacity-50"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder={loading ? loadingPlaceholder : placeholder}
        disabled={loading}
      />
      <button
        type="submit"
        disabled={loading || !input.trim()}
        className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40
                   rounded text-sm font-medium transition-colors"
      >
        Send
      </button>
    </form>
  )
}
