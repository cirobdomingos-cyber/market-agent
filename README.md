# MarketCoach

AI investment intelligence platform that monitors markets, generates structured investment theses, auto-resolves predictions against real prices, and provides interactive coaching through a conversational agent. Includes paper trading via Alpaca.

## How It Works

1. **Every 4 hours**, the intelligence pipeline runs automatically:
   - **NewsAgent** searches for financial news via DuckDuckGo, then asks Claude to extract structured sentiment signals
   - **AnalysisAgent** synthesizes signals into 3-5 market theses with direction, confidence, timeframe, and key risks
   - **Thesis Resolution** checks expired theses against actual price movement (yfinance) and scores prediction accuracy

2. **The Coach** is a conversational Claude agent that:
   - Teaches you about markets using your actual portfolio and thesis context
   - Fetches real-time prices, technicals, and fundamentals via the `market_data` tool
   - Searches for current news via `web_search`
   - Auto-detects EXPLAIN mode ("what is RSI?") vs ANALYSE mode ("should I buy NVDA?")
   - Never gives direct buy/sell recommendations — frames everything as thesis + conditions
   - Learns your risk tolerance, interests, and trading style via the MemoryAgent

3. **Alpaca paper trading** lets you act on theses with zero risk — all trades execute against a paper account.

## Architecture

```
React Frontend (Vite + Tailwind)
  Dashboard | Coach (chat) | Portfolio
       |
       | HTTP (axios)
       v
FastAPI Backend (bearer-token auth)
       |
       v
  Orchestrator
  |-- NewsAgent ---------> DuckDuckGo Search --> Claude (signal extraction)
  |-- AnalysisAgent -----> Claude (thesis generation from signals)
  |   |-- resolve_expired_theses() --> yfinance (price comparison)
  |-- CoachAgent --------> Claude + web_search + market_data tools
  |   |-- session history (last 20 messages)
  |   |-- context injection (portfolio, theses, signals, accuracy)
  |-- MemoryAgent -------> Claude (extract user profile from conversations)
       |
       v
  Tools Layer
  |-- web_search.py -----> DuckDuckGo (news + text)
  |-- market_data.py ----> yfinance (quotes, technicals, fundamentals, history, compare)
  |-- alpaca.py ---------> Alpaca SDK (paper trading, positions, orders)
       |
       v
  SQLite (SQLAlchemy ORM)
  Signal | Thesis | ChatMessage | UserMemory | Position | AutoRule
```

## Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.12, FastAPI, SQLAlchemy, APScheduler |
| AI | Anthropic SDK (Claude Sonnet), tool-use agentic loop |
| Market Data | yfinance (quotes, technicals, fundamentals, price history) |
| News | DuckDuckGo Search (no API key required) |
| Trading | Alpaca API (paper trading) |
| Frontend | React 18, Vite, Tailwind CSS, Recharts, react-markdown |
| Testing | pytest, FastAPI TestClient, in-memory SQLite |

## Getting Started

### Prerequisites
- Python 3.12+
- Node.js 18+
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- Alpaca API keys (optional — [alpaca.markets](https://alpaca.markets))

### Backend

```bash
cd marketcoach
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY (required), ALPACA keys (optional)

pip install -r requirements.txt
uvicorn backend.main:app --reload
```

API at `http://localhost:8000`. Docs at `/docs`.

### Frontend

```bash
cd marketcoach/frontend
npm install
npm run dev
```

UI at `http://localhost:5173`.

### Run Tests

```bash
cd marketcoach
pytest tests/ -v
```

53 tests, ~2 seconds, zero external dependencies.

## API Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Liveness check + Alpaca connection status |
| GET | `/signals` | No | Paginated signal feed (filter by ticker, hours) |
| GET | `/theses` | No | Paginated theses (open or all, includes accuracy) |
| GET | `/accuracy` | No | Thesis accuracy stats (SQL aggregates) |
| POST | `/pipeline/run` | Yes | Trigger news + analysis + resolution pipeline |
| POST | `/chat` | Yes | Send message to coach (returns markdown response) |
| GET | `/chat/{session_id}` | No | Fetch session message history |
| GET | `/portfolio` | Yes | Paper positions + account summary (Alpaca) |
| GET | `/portfolio/orders` | Yes | Recent order history |
| GET | `/profile` | No | User memory / preferences |
| POST | `/profile` | Yes | Manually set a user preference |

## Authentication

Set `API_SECRET` in `.env` to enable bearer-token auth. When empty (dev mode), auth is bypassed.

```bash
curl -H "Authorization: Bearer YOUR_SECRET" http://localhost:8000/pipeline/run -X POST
```

## Key Design Decisions

- **Agentic loop pattern** — `send -> tool_use -> tool_result -> loop` with a hard iteration cap. Same pattern used in all production Claude tool-use agents.
- **Structured output pipeline** — LLM -> JSON -> Pydantic validation -> SQLAlchemy ORM. The standard approach for LLM-to-DB pipelines.
- **Closed feedback loop** — generate thesis -> wait for expiry -> compare prediction vs reality -> store accuracy. Same pattern as ML model monitoring.
- **Context stuffing over RAG** — portfolio, theses, signals injected directly into the system prompt. Simpler and more deterministic than vector search for this data volume.
- **Defense-in-depth for trading** — paper_only enforced at the Alpaca client layer, tool executor, API endpoint, and DB model default.

## License

[MIT](LICENSE)
