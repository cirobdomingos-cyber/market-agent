from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str

    # ── Broker selection ─────────────────────────────────────────────────────
    # Which broker MarketCoach talks to. "alpaca" by default (the original
    # integration); "ibkr" for Interactive Brokers via ib_insync. Brazilian
    # residents need IBKR because Alpaca won't onboard them for live accounts.
    # The selection happens once at startup; switch by editing .env and restarting.
    broker_provider: str = "alpaca"

    # ── Alpaca credentials ───────────────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # ── IBKR connection (only used when broker_provider == "ibkr") ───────────
    # IBKR requires a local IB Gateway / TWS process, not direct internet.
    # Default port 7497 = paper, 7496 = live. Client ID can be anything 1-32
    # but must be unique per simultaneously-connected client.
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # ── Real-capital safety gate ─────────────────────────────────────────────
    # Defaults to paper. Going live requires BOTH flags flipped — a single
    # misconfiguration (env var typo, accidental merge) can never put real
    # money at risk on its own. Defence in depth. Applies to whichever broker
    # is active — the gate is broker-agnostic.
    alpaca_paper: bool = True
    # Must equal the exact string "I understand this uses real capital" for
    # live mode to activate. Deliberately verbose so nobody sets it by reflex.
    alpaca_live_confirmation: str = ""

    database_url: str = "sqlite:///./marketcoach.db"

    api_secret: str = ""  # Set to enable bearer-token auth; empty = dev mode (no auth)
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    news_cache_ttl_minutes: int = 30
    agent_run_interval_hours: int = 4

    # ── Auto news reactions ──────────────────────────────────────────────────
    # When the intelligence pipeline writes high-impact signals, the orchestrator
    # auto-fires a TradingAdvisor "react to" call. Cost-control via dedupe +
    # per-run cap. Set news_reactions_enabled=False to disable entirely.
    news_reactions_enabled: bool = True
    news_reaction_min_confidence: float = 0.8
    news_reactions_per_run_max: int = 5
    news_reaction_dedupe_hours: int = 6
    news_reaction_session_id: str = "news-reactions-auto"

    # ── Auto position-change reviews ─────────────────────────────────────────
    # Polling job that watches broker positions and auto-fires advisor reviews
    # when something meaningful changes (new position, closed, qty changed, big
    # P&L move). Reviews are persisted in news_reactions with trigger_reason
    # values in {position_opened, position_closed, position_changed, pnl_threshold}.
    position_reviews_enabled: bool = True
    position_poll_interval_minutes: int = 5
    position_qty_change_threshold_pct: float = 5.0   # qty change > N% → review
    position_pnl_change_threshold_pct: float = 10.0  # P&L move > N pp → review
    position_review_dedupe_hours: int = 4
    position_reviews_per_poll_max: int = 5
    position_review_session_id: str = "position-reviews-auto"

    # Weekly plan cron (APScheduler CronTrigger format)
    weekly_plan_enabled: bool = True
    weekly_plan_day_of_week: str = "sun"   # mon, tue, wed, thu, fri, sat, sun
    weekly_plan_hour: int = 21              # 24h local time
    weekly_plan_minute: int = 0
    weekly_plan_session_id: str = "weekly-plan-scheduled"

    # Morning brief cron — fires before market open every weekday so the
    # brief is waiting on the user's phone when they wake up. 06:00 BRT
    # ≈ 05:00 EDT, ~4.5 hours before US market open. Plenty of time to
    # read, plan, and execute pre-market or at the bell.
    morning_brief_enabled: bool = True
    morning_brief_day_of_week: str = "mon-fri"
    morning_brief_hour: int = 6
    morning_brief_minute: int = 0
    morning_brief_session_id: str = "morning-brief-scheduled"

    default_watchlist: str = "AAPL,MSFT,NVDA,GOOGL,AMZN,TSLA,META,SPY,QQQ"

    @property
    def watchlist(self) -> list[str]:
        return [t.strip().upper() for t in self.default_watchlist.split(",") if t.strip()]

    # ── Trading mode helpers ─────────────────────────────────────────────────
    # ClassVar so pydantic-settings treats this as a constant, not a field.
    LIVE_CONFIRMATION_PHRASE: ClassVar[str] = "I understand this uses real capital"

    @property
    def is_live_mode(self) -> bool:
        """
        True only when BOTH alpaca_paper is False AND the exact confirmation
        phrase is set. Either switch alone returns False.
        """
        return (
            self.alpaca_paper is False
            and self.alpaca_live_confirmation == self.LIVE_CONFIRMATION_PHRASE
        )

    @property
    def trading_mode(self) -> str:
        """Human-readable mode label: 'paper' or 'live'."""
        return "live" if self.is_live_mode else "paper"


settings = Settings()
