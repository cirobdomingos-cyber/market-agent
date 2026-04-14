import { useState, useEffect, useCallback } from 'react'
import axios from 'axios'

const API = '/api'

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

const DAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// Build an ISO date string from year, 0-indexed month, and day.
function isoDate(year, month0, day) {
  return `${year}-${String(month0 + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

export default function Calendar() {
  const today = new Date()
  const [year, setYear] = useState(today.getFullYear())
  const [month, setMonth] = useState(today.getMonth()) // 0-indexed in JS
  const [events, setEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null) // { date, event }

  const fetchEvents = useCallback(async (y, m0) => {
    setLoading(true)
    try {
      const res = await axios.get(`${API}/events`, {
        params: { year: y, month: m0 + 1 }, // API uses 1-indexed month
      })
      setEvents(res.data)
    } catch (err) {
      console.error('Failed to fetch events:', err)
      setEvents([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchEvents(year, month)
    setSelected(null)
  }, [year, month, fetchEvents])

  const prevMonth = () => {
    if (month === 0) { setYear(y => y - 1); setMonth(11) }
    else setMonth(m => m - 1)
  }

  const nextMonth = () => {
    if (month === 11) { setYear(y => y + 1); setMonth(0) }
    else setMonth(m => m + 1)
  }

  const goToday = () => {
    setYear(today.getFullYear())
    setMonth(today.getMonth())
  }

  // Group events by day-of-month for O(1) lookup during render.
  // Parse date at noon local time to avoid timezone-induced off-by-one shifts.
  const eventsByDay = {}
  for (const ev of events) {
    const d = new Date(ev.date + 'T12:00:00')
    if (d.getMonth() === month && d.getFullYear() === year) {
      const day = d.getDate()
      if (!eventsByDay[day]) eventsByDay[day] = []
      eventsByDay[day].push(ev)
    }
  }

  const firstDayOfWeek = new Date(year, month, 1).getDay()
  const daysInMonth = new Date(year, month + 1, 0).getDate()

  // Pad the start with nulls so day 1 lands on the right weekday column.
  const cells = [
    ...Array(firstDayOfWeek).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ]
  // Pad the end to complete the last row.
  while (cells.length % 7 !== 0) cells.push(null)

  const todayStr = isoDate(today.getFullYear(), today.getMonth(), today.getDate())
  const viewingCurrentMonth =
    year === today.getFullYear() && month === today.getMonth()

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Market Calendar</h1>
        <div className="flex items-center gap-4 text-sm text-gray-400">
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-indigo-500" />
            Earnings
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-amber-500" />
            FOMC
          </span>
        </div>
      </div>

      {/* Month navigation */}
      <div className="flex items-center gap-3">
        <button
          onClick={prevMonth}
          className="px-2.5 py-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white
                     transition-colors text-xl leading-none"
          aria-label="Previous month"
        >
          ‹
        </button>
        <span className="text-lg font-semibold w-44 text-center">
          {MONTH_NAMES[month]} {year}
        </span>
        <button
          onClick={nextMonth}
          className="px-2.5 py-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white
                     transition-colors text-xl leading-none"
          aria-label="Next month"
        >
          ›
        </button>
        {!viewingCurrentMonth && (
          <button
            onClick={goToday}
            className="ml-2 px-3 py-1 text-xs rounded border border-gray-700
                       text-gray-400 hover:text-white hover:border-gray-500 transition-colors"
          >
            Today
          </button>
        )}
      </div>

      {/* Calendar grid */}
      {loading ? (
        <p className="text-gray-500 text-sm">Loading events...</p>
      ) : (
        <div className="rounded-lg border border-gray-800 overflow-hidden">
          {/* Day-of-week header row */}
          <div className="grid grid-cols-7 bg-gray-900 border-b border-gray-800">
            {DAY_LABELS.map(label => (
              <div
                key={label}
                className="py-2 text-center text-xs font-medium text-gray-500 uppercase tracking-wide"
              >
                {label}
              </div>
            ))}
          </div>

          {/* Day cells */}
          <div className="grid grid-cols-7 divide-x divide-y divide-gray-800">
            {cells.map((day, idx) => {
              if (day === null) {
                return (
                  <div
                    key={`empty-${idx}`}
                    className="bg-gray-950 min-h-[88px]"
                  />
                )
              }

              const dateStr = isoDate(year, month, day)
              const dayEvents = eventsByDay[day] || []
              const isToday = dateStr === todayStr

              return (
                <div
                  key={dateStr}
                  className="bg-gray-950 min-h-[88px] p-1.5 hover:bg-gray-900/60 transition-colors"
                >
                  <span
                    className={`text-xs font-medium inline-flex items-center justify-center
                                w-6 h-6 rounded-full mb-1 select-none
                                ${isToday
                                  ? 'bg-indigo-600 text-white'
                                  : 'text-gray-500'
                                }`}
                  >
                    {day}
                  </span>

                  <div className="space-y-0.5">
                    {dayEvents.map((ev, i) => (
                      <button
                        key={`${dateStr}-${i}`}
                        onClick={() =>
                          setSelected(
                            selected?.date === dateStr && selected?.idx === i
                              ? null
                              : { date: dateStr, event: ev, idx: i },
                          )
                        }
                        className={`w-full text-left text-xs px-1.5 py-0.5 rounded truncate
                                    transition-colors font-medium
                                    ${ev.type === 'fomc'
                                      ? 'bg-amber-900/30 text-amber-300 hover:bg-amber-900/60'
                                      : 'bg-indigo-900/30 text-indigo-300 hover:bg-indigo-900/60'
                                    }`}
                        title={ev.detail || ev.title}
                      >
                        {ev.title}
                      </button>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Event detail panel — appears below calendar when an event is selected */}
      {selected && (
        <div className="rounded-lg border border-gray-700 bg-gray-900 p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span
                  className={`text-xs font-semibold px-2 py-0.5 rounded-full uppercase tracking-wide
                              ${selected.event.type === 'fomc'
                                ? 'bg-amber-900/40 text-amber-300'
                                : 'bg-indigo-900/40 text-indigo-300'
                              }`}
                >
                  {selected.event.type}
                </span>
                <span className="text-white font-semibold">{selected.event.title}</span>
              </div>
              <p className="text-gray-400 text-sm">{selected.date}</p>
              {selected.event.detail && (
                <p className="text-gray-300 text-sm">{selected.event.detail}</p>
              )}
              {selected.event.type === 'earnings' && (
                <p className="text-gray-500 text-xs">
                  Earnings release — confirm exact time (pre-market or after-hours) closer to the date.
                </p>
              )}
            </div>
            <button
              onClick={() => setSelected(null)}
              className="text-gray-600 hover:text-white transition-colors text-xl leading-none flex-shrink-0"
              aria-label="Close"
            >
              ×
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
