import { useState, useEffect, useRef } from 'react'
import axios from 'axios'
import ReactMarkdown from 'react-markdown'
import ChatWindow from '../components/ChatWindow'

const API = '/api'

export default function Coach() {
  const [sessionId] = useState(() => `session-${Date.now()}`)
  const [messages, setMessages] = useState([])
  const [accuracy, setAccuracy] = useState(null)
  const [openTheses, setOpenTheses] = useState(0)
  const [profile, setProfile] = useState([])
  const messagesEndRef = useRef(null)

  useEffect(() => {
    async function fetchContext() {
      try {
        const [accRes, thRes, profRes] = await Promise.all([
          axios.get(`${API}/accuracy`),
          axios.get(`${API}/theses?open_only=true&limit=5`),
          axios.get(`${API}/profile`),
        ])
        setAccuracy(accRes.data)
        setOpenTheses(thRes.data.length)
        setProfile(profRes.data.memories || [])
      } catch (err) {
        console.error('Failed to fetch context:', err)
      }
    }
    fetchContext()
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async (message) => {
    setMessages((prev) => [...prev, { role: 'user', content: message }])

    try {
      const res = await axios.post(`${API}/chat`, {
        session_id: sessionId,
        message,
      })
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: res.data.response },
      ])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: 'Sorry, something went wrong. Try again.' },
      ])
    }
  }

  return (
    <div className="flex flex-col lg:flex-row gap-4 lg:gap-6 lg:h-[80vh]">
      <div className="flex-1 flex flex-col border border-gray-800 rounded-lg min-h-[60vh] lg:min-h-0">
        <div className="flex-1 p-4 overflow-y-auto space-y-4">
          {messages.length === 0 && (
            <div className="text-gray-500 text-sm space-y-2">
              <p>Ask the coach anything about markets, your portfolio, or current theses.</p>
              <div className="flex flex-wrap gap-2 mt-3">
                {[
                  "What do the current theses suggest?",
                  "Explain RSI and how to use it",
                  "Review my portfolio risk",
                  "What's happening with NVDA?",
                ].map((q) => (
                  <button
                    key={q}
                    onClick={() => handleSend(q)}
                    className="px-3 py-1.5 text-xs bg-gray-800 hover:bg-gray-700 rounded-full
                               text-gray-300 transition-colors"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div
              key={i}
              className={`text-sm ${
                m.role === 'user' ? 'text-gray-300' : 'text-indigo-200'
              }`}
            >
              <span className="text-xs text-gray-500 uppercase font-medium">
                {m.role === 'user' ? 'You' : 'Coach'}
              </span>
              <div className="mt-1 prose prose-invert prose-sm max-w-none">
                <ReactMarkdown>{m.content}</ReactMarkdown>
              </div>
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>
        <div className="border-t border-gray-800 p-4">
          <ChatWindow sessionId={sessionId} onSend={handleSend} />
        </div>
      </div>

      <div className="w-full lg:w-64 space-y-4 text-sm text-gray-400">
        <div>
          <p className="font-semibold text-gray-300 mb-2">Context</p>
          <div className="space-y-1">
            <p>Open theses: <span className="text-white">{openTheses}</span></p>
            <p>
              Accuracy:{' '}
              <span className="text-white">
                {accuracy && accuracy.accuracy_pct !== null
                  ? `${accuracy.accuracy_pct}%`
                  : '--'}
              </span>
            </p>
          </div>
        </div>

        {profile.length > 0 && (
          <div>
            <p className="font-semibold text-gray-300 mb-2">Your Profile</p>
            <div className="space-y-1.5">
              {profile.map((m) => (
                <div key={m.key} className="text-xs">
                  <span className="text-gray-500">{m.key.replace(/_/g, ' ')}:</span>{' '}
                  <span className="text-gray-300">{m.value}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
