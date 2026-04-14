import { useEffect, useState } from 'react'
import axios from 'axios'
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import TradeIdeas from './pages/TradeIdeas'
import Coach from './pages/Coach'
import Advisor from './pages/Advisor'
import Portfolio from './pages/Portfolio'
import Backtest from './pages/Backtest'
import Calendar from './pages/Calendar'
import WeeklyPlan from './pages/WeeklyPlan'
import NewsReactions from './pages/NewsReactions'
import MorningBrief from './pages/MorningBrief'
import ExecutedOrders from './pages/ExecutedOrders'
import Journal from './pages/Journal'

const API = '/api'
const UNREAD_POLL_MS = 60_000

export default function App() {
  const [mode, setMode] = useState(null)
  const [unreadCount, setUnreadCount] = useState(0)
  const [journalActionCount, setJournalActionCount] = useState(0)

  useEffect(() => {
    axios
      .get(`${API}/mode`)
      .then((res) => setMode(res.data))
      .catch((err) => console.error('Failed to fetch mode:', err))
  }, [])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const [reactions, journal] = await Promise.all([
          axios.get(`${API}/news-reactions/unread-count`),
          axios.get(`${API}/journal/action-needed-count`),
        ])
        if (!cancelled) {
          setUnreadCount(reactions.data.unread || 0)
          setJournalActionCount(journal.data.count || 0)
        }
      } catch (err) {
        // Silent — endpoint may be temporarily down; nav still works
      }
    }
    poll()
    const id = setInterval(poll, UNREAD_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const isLive = mode?.is_live === true

  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-950 text-gray-100">
        {isLive && (
          <div className="bg-red-700 text-white text-center py-2 font-bold text-sm tracking-wide border-b-2 border-red-500">
            ⚠️ LIVE TRADING MODE — REAL CAPITAL AT RISK ⚠️
          </div>
        )}
        <nav
          className={`border-b px-4 sm:px-6 py-3 flex items-center gap-4 sm:gap-6 overflow-x-auto whitespace-nowrap ${
            isLive ? 'border-red-800 bg-red-950/30' : 'border-gray-800'
          }`}
        >
          <span
            className={`font-bold text-lg ${
              isLive ? 'text-red-300' : 'text-indigo-400'
            }`}
          >
            MarketCoach
            {mode && (
              <span
                className={`ml-2 text-xs font-mono px-1.5 py-0.5 rounded ${
                  isLive
                    ? 'bg-red-700 text-white'
                    : 'bg-gray-800 text-gray-400'
                }`}
              >
                {mode.mode}
              </span>
            )}
          </span>
          <NavLink
            to="/"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Dashboard
          </NavLink>
          <NavLink
            to="/trades"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Trade Ideas
          </NavLink>
          <NavLink
            to="/coach"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Coach
          </NavLink>
          <NavLink
            to="/advisor"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Advisor
          </NavLink>
          <NavLink
            to="/portfolio"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Portfolio
          </NavLink>
          <NavLink
            to="/orders"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Orders
          </NavLink>
          <NavLink
            to="/journal"
            className={({ isActive }) =>
              `relative ${
                isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
              }`
            }
          >
            Journal
            {journalActionCount > 0 && (
              <span className="ml-1.5 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 text-xs font-bold bg-amber-600 text-white rounded-full">
                {journalActionCount > 99 ? '99+' : journalActionCount}
              </span>
            )}
          </NavLink>
          <NavLink
            to="/backtest"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Backtest
          </NavLink>
          <NavLink
            to="/calendar"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Calendar
          </NavLink>
          <NavLink
            to="/weekly-plan"
            className={({ isActive }) =>
              isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
            }
          >
            Weekly Plan
          </NavLink>
          <NavLink
            to="/news-reactions"
            className={({ isActive }) =>
              `relative ${
                isActive ? 'text-white font-medium' : 'text-gray-400 hover:text-white'
              }`
            }
          >
            News
            {unreadCount > 0 && (
              <span className="ml-1.5 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 text-xs font-bold bg-indigo-500 text-white rounded-full">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </NavLink>
        </nav>

        <main className="p-4 sm:p-6">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/trades" element={<TradeIdeas />} />
            <Route path="/coach" element={<Coach />} />
            <Route path="/advisor" element={<Advisor />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/calendar" element={<Calendar />} />
            <Route path="/weekly-plan" element={<WeeklyPlan />} />
            <Route path="/news-reactions" element={<NewsReactions />} />
            <Route path="/morning-brief" element={<MorningBrief />} />
            <Route path="/orders" element={<ExecutedOrders />} />
            <Route path="/journal" element={<Journal />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
