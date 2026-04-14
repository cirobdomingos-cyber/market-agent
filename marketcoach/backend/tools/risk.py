"""
Position sizing and risk management calculations.

Uses a modified Kelly Criterion for position sizing, with portfolio-level
risk constraints. This is the math that turns a thesis into a trade idea.

Interview angle:
  Kelly Criterion is a standard quant finance concept. The modification here
  (half-Kelly with caps) is the industry-standard approach to avoid the
  "bet the farm" problem with full Kelly sizing. Full Kelly maximizes
  long-term growth rate but has enormous variance — half-Kelly gives ~75%
  of the growth with far less drawdown risk.
"""

from __future__ import annotations


# -- Risk tolerance multipliers ------------------------------------------------
# Conservative investors size down, aggressive size up. These scale the
# half-Kelly output so the raw math stays clean.
_TOLERANCE_MULTIPLIERS: dict[str, float] = {
    "conservative": 0.5,
    "moderate": 1.0,
    "aggressive": 1.5,
}


def calculate_position_size(
    confidence: float,
    risk_reward_ratio: float,
    portfolio_value: float,
    max_position_pct: float = 0.10,
    max_portfolio_risk_pct: float = 0.02,
    risk_tolerance: str = "moderate",
) -> dict:
    """
    Calculate position size using modified half-Kelly Criterion.

    The Kelly formula determines the optimal fraction of capital to wager:

        Kelly fraction = (p * b - q) / b

    where:
        p = probability of winning (mapped from *confidence*)
        q = 1 - p (probability of losing)
        b = risk/reward ratio (how much you win per unit risked)

    We then apply *half-Kelly* (divide by 2) because:
      - Full Kelly assumes perfect probability estimates — we never have those.
      - Half-Kelly captures ~75 % of the theoretical growth rate with
        dramatically lower variance and max drawdown.

    After the Kelly math, two hard caps are enforced:
      1. ``max_position_pct`` — no single position exceeds this % of portfolio.
      2. ``max_portfolio_risk_pct`` — the dollar amount at risk (assuming a
         total loss of the position) never exceeds this % of portfolio.

    Parameters
    ----------
    confidence : float
        Thesis confidence in [0.0, 1.0]. Treated as win probability.
    risk_reward_ratio : float
        Expected reward divided by expected risk (must be > 0).
    portfolio_value : float
        Total account value in USD.
    max_position_pct : float
        Maximum fraction of portfolio in one position (default 10 %).
    max_portfolio_risk_pct : float
        Maximum portfolio risk per trade as a fraction (default 2 %).
    risk_tolerance : str
        One of ``"conservative"``, ``"moderate"``, ``"aggressive"``.

    Returns
    -------
    dict
        shares_value, position_pct, max_loss_dollars, max_loss_pct,
        kelly_fraction, sizing_method.
    """
    # --- Input validation -----------------------------------------------------
    confidence = max(0.0, min(1.0, confidence))
    risk_reward_ratio = max(0.001, risk_reward_ratio)  # avoid division by zero
    portfolio_value = max(0.0, portfolio_value)
    tolerance_mult = _TOLERANCE_MULTIPLIERS.get(risk_tolerance, 1.0)

    # --- Kelly Criterion ------------------------------------------------------
    p = confidence
    q = 1.0 - p
    b = risk_reward_ratio

    kelly_fraction = (p * b - q) / b if b > 0 else 0.0

    # Negative Kelly means expected value is negative — don't trade.
    if kelly_fraction <= 0:
        return {
            "shares_value": 0.0,
            "position_pct": 0.0,
            "max_loss_dollars": 0.0,
            "max_loss_pct": 0.0,
            "kelly_fraction": kelly_fraction,
            "sizing_method": (
                "Kelly fraction <= 0 — negative expected value, no trade."
            ),
        }

    # Half-Kelly, then scale by risk tolerance.
    adjusted_fraction = kelly_fraction * 0.5 * tolerance_mult

    # --- Apply hard caps ------------------------------------------------------
    sizing_notes: list[str] = []

    # Cap 1: max position size as % of portfolio.
    if adjusted_fraction > max_position_pct:
        adjusted_fraction = max_position_pct
        sizing_notes.append(
            f"Capped at max_position_pct ({max_position_pct:.0%})"
        )

    shares_value = adjusted_fraction * portfolio_value

    # Cap 2: max portfolio risk per trade.
    max_risk_dollars = max_portfolio_risk_pct * portfolio_value
    if shares_value > max_risk_dollars:
        shares_value = max_risk_dollars
        adjusted_fraction = shares_value / portfolio_value if portfolio_value > 0 else 0.0
        sizing_notes.append(
            f"Capped at max_portfolio_risk ({max_portfolio_risk_pct:.0%} = "
            f"${max_risk_dollars:,.2f})"
        )

    if not sizing_notes:
        sizing_notes.append("Half-Kelly with risk-tolerance adjustment")

    return {
        "shares_value": round(shares_value, 2),
        "position_pct": round(adjusted_fraction, 4),
        "max_loss_dollars": round(shares_value, 2),  # worst case: total loss
        "max_loss_pct": round(adjusted_fraction, 4),
        "kelly_fraction": round(kelly_fraction, 4),
        "sizing_method": "; ".join(sizing_notes),
    }


def calculate_stop_loss(
    entry_price: float,
    direction: str,
    atr: float | None,
    support_level: float | None = None,
    resistance_level: float | None = None,
) -> float:
    """
    Calculate stop-loss price.

    Priority order:
      1. **ATR-based** (if ``atr`` is provided and > 0):
         - Long:  ``entry_price - 2 * ATR``
         - Short: ``entry_price + 2 * ATR``
         The 2x multiplier gives room for normal volatility while still
         cutting losses on a genuine move against you.

      2. **Technical level** (if support/resistance provided):
         - Long:  just below support (``support * 0.99``)
         - Short: just above resistance (``resistance * 1.01``)
         The 1 % buffer avoids getting stopped by a wick that touches
         exactly the level before reversing.

      3. **Fallback**: 5 % from entry.

    Parameters
    ----------
    entry_price : float
        The planned entry price.
    direction : str
        ``"long"`` or ``"short"``.
    atr : float | None
        Average True Range for the asset (14-period by convention).
    support_level : float | None
        Nearest support price (relevant for longs).
    resistance_level : float | None
        Nearest resistance price (relevant for shorts).

    Returns
    -------
    float
        The stop-loss price, rounded to 2 decimals.
    """
    is_long = direction.lower() == "long"

    # Priority 1: ATR-based stop
    if atr is not None and atr > 0:
        if is_long:
            return round(entry_price - 2.0 * atr, 2)
        return round(entry_price + 2.0 * atr, 2)

    # Priority 2: Technical levels
    if is_long and support_level is not None and support_level > 0:
        return round(support_level * 0.99, 2)

    if not is_long and resistance_level is not None and resistance_level > 0:
        return round(resistance_level * 1.01, 2)

    # Priority 3: Fallback — 5 % from entry
    if is_long:
        return round(entry_price * 0.95, 2)
    return round(entry_price * 1.05, 2)


def calculate_take_profit(
    entry_price: float,
    stop_loss: float,
    direction: str,
    min_risk_reward: float = 2.0,
    resistance_level: float | None = None,
    support_level: float | None = None,
) -> float:
    """
    Calculate take-profit price ensuring a minimum risk/reward ratio.

    The minimum target is derived from the stop-loss distance:

        risk = |entry_price - stop_loss|
        min_reward = risk * min_risk_reward

    For longs:  ``target = entry + min_reward``
    For shorts: ``target = entry - min_reward``

    If a resistance level (for longs) or support level (for shorts) sits
    *beyond* the minimum target, we use that technical level instead —
    it represents a more realistic profit zone the market has already
    respected.

    Parameters
    ----------
    entry_price : float
        Planned entry price.
    stop_loss : float
        Already-calculated stop-loss price.
    direction : str
        ``"long"`` or ``"short"``.
    min_risk_reward : float
        Minimum acceptable reward-to-risk ratio (default 2:1).
    resistance_level : float | None
        Nearest resistance (relevant for long take-profit).
    support_level : float | None
        Nearest support (relevant for short take-profit).

    Returns
    -------
    float
        The take-profit price, rounded to 2 decimals.
    """
    is_long = direction.lower() == "long"
    risk = abs(entry_price - stop_loss)
    min_reward = risk * min_risk_reward

    if is_long:
        min_target = entry_price + min_reward
        # Use resistance if it's beyond the minimum target
        if resistance_level is not None and resistance_level > min_target:
            return round(resistance_level, 2)
        return round(min_target, 2)

    # Short
    min_target = entry_price - min_reward
    # Use support if it's below (beyond) the minimum target
    if support_level is not None and support_level < min_target:
        return round(support_level, 2)
    return round(min_target, 2)


def calculate_risk_reward_ratio(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
) -> float:
    """
    Calculate the risk/reward ratio for a trade.

    Formula:
        risk    = |entry - stop_loss|
        reward  = |take_profit - entry|
        ratio   = reward / risk

    A ratio of 2.0 means you stand to gain $2 for every $1 risked.

    Parameters
    ----------
    entry_price : float
        Planned entry price.
    stop_loss : float
        Stop-loss price.
    take_profit : float
        Take-profit price.
    direction : str
        ``"long"`` or ``"short"`` (included for API consistency; the math
        uses absolute values so direction doesn't change the result).

    Returns
    -------
    float
        The reward-to-risk ratio, rounded to 2 decimals.
        Returns 0.0 if risk is zero (entry == stop_loss).
    """
    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)

    if risk == 0:
        return 0.0

    return round(reward / risk, 2)


def classify_horizon(timeframe: str) -> str:
    """
    Convert a thesis timeframe string to a horizon category.

    Mapping:
      - ``"1-3 days"``   -> ``"short"``
      - ``"3-10 days"``  -> ``"short"``
      - ``"1-4 weeks"``  -> ``"medium"``
      - ``"2-4 weeks"``  -> ``"medium"``
      - ``"1-3 months"`` -> ``"long"``

    Falls back to ``"medium"`` for unrecognized strings, since that's the
    safest default for position sizing.

    Parameters
    ----------
    timeframe : str
        Human-readable timeframe from a thesis (e.g. ``"1-3 days"``).

    Returns
    -------
    str
        ``"short"``, ``"medium"``, or ``"long"``.
    """
    normalized = timeframe.strip().lower()

    if "day" in normalized:
        return "short"
    if "week" in normalized:
        return "medium"
    if "month" in normalized:
        return "long"

    # Fallback: safest default
    return "medium"


def portfolio_risk_check(
    new_idea: dict,
    existing_positions: list[dict],
    existing_ideas: list[dict],
    portfolio_value: float,
    max_total_exposure_pct: float = 0.60,
    max_sector_exposure_pct: float = 0.25,
    max_correlated_pct: float = 0.30,
) -> dict:
    """
    Portfolio-level risk check before adding a new trade idea.

    Evaluates three constraints:

    1. **Total exposure**: sum of all position values + new idea must stay
       below ``max_total_exposure_pct`` of portfolio. Keeps cash buffer
       for drawdowns and new opportunities.

    2. **Sector concentration**: positions in the same sector as the new
       idea must not exceed ``max_sector_exposure_pct``. Prevents sector
       blow-up risk (e.g. all tech during a rotation).

    3. **Correlated positions**: positions in the same *asset class* or
       direction that would move together. Uses a simple heuristic —
       same-sector + same-direction = correlated.

    Parameters
    ----------
    new_idea : dict
        Must contain keys: ``symbol``, ``shares_value``, ``sector``
        (optional), ``direction`` (optional, defaults to ``"long"``).
    existing_positions : list[dict]
        Active positions. Each dict should have ``symbol``,
        ``market_value``, ``sector`` (optional), ``side`` (optional).
    existing_ideas : list[dict]
        Pending (not yet executed) trade ideas. Each dict should have
        ``symbol``, ``shares_value``, ``sector`` (optional),
        ``direction`` (optional).
    portfolio_value : float
        Total account value in USD.
    max_total_exposure_pct : float
        Maximum total invested as fraction of portfolio (default 60 %).
    max_sector_exposure_pct : float
        Maximum in one sector as fraction of portfolio (default 25 %).
    max_correlated_pct : float
        Maximum in correlated positions as fraction (default 30 %).

    Returns
    -------
    dict
        approved (bool), warnings (list[str]), rejections (list[str]),
        current_exposure_pct (float).
    """
    warnings: list[str] = []
    rejections: list[str] = []

    if portfolio_value <= 0:
        return {
            "approved": False,
            "warnings": [],
            "rejections": ["Portfolio value is zero or negative."],
            "current_exposure_pct": 0.0,
        }

    new_value = new_idea.get("shares_value", 0.0)
    new_sector = (new_idea.get("sector") or "unknown").lower()
    new_direction = (new_idea.get("direction") or "long").lower()
    new_symbol = new_idea.get("symbol", "").upper()

    # --- Calculate current exposure -------------------------------------------
    total_exposure = 0.0
    sector_exposure: dict[str, float] = {}
    correlated_exposure = 0.0  # same sector + same direction as new idea

    # Active positions
    for pos in existing_positions:
        mv = abs(pos.get("market_value", 0.0))
        total_exposure += mv

        sector = (pos.get("sector") or "unknown").lower()
        sector_exposure[sector] = sector_exposure.get(sector, 0.0) + mv

        pos_side = (pos.get("side") or "long").lower()
        if sector == new_sector and pos_side == new_direction:
            correlated_exposure += mv

    # Pending ideas (not yet executed, but committed capital)
    for idea in existing_ideas:
        sv = abs(idea.get("shares_value", 0.0))
        total_exposure += sv

        sector = (idea.get("sector") or "unknown").lower()
        sector_exposure[sector] = sector_exposure.get(sector, 0.0) + sv

        idea_dir = (idea.get("direction") or "long").lower()
        if sector == new_sector and idea_dir == new_direction:
            correlated_exposure += sv

    current_exposure_pct = round(total_exposure / portfolio_value, 4)

    # --- Check: duplicate symbol ----------------------------------------------
    all_symbols = {p.get("symbol", "").upper() for p in existing_positions}
    all_symbols |= {i.get("symbol", "").upper() for i in existing_ideas}
    if new_symbol and new_symbol in all_symbols:
        warnings.append(
            f"Already have a position or pending idea in {new_symbol}."
        )

    # --- Check 1: Total exposure ----------------------------------------------
    projected_total = total_exposure + new_value
    projected_total_pct = projected_total / portfolio_value

    if projected_total_pct > max_total_exposure_pct:
        rejections.append(
            f"Total exposure would be {projected_total_pct:.1%} "
            f"(limit {max_total_exposure_pct:.0%}). "
            f"Current: ${total_exposure:,.0f}, "
            f"new: ${new_value:,.0f}."
        )
    elif projected_total_pct > max_total_exposure_pct * 0.85:
        warnings.append(
            f"Approaching total exposure limit: {projected_total_pct:.1%} "
            f"of {max_total_exposure_pct:.0%}."
        )

    # --- Check 2: Sector concentration ----------------------------------------
    current_sector_value = sector_exposure.get(new_sector, 0.0)
    projected_sector = current_sector_value + new_value
    projected_sector_pct = projected_sector / portfolio_value

    if new_sector != "unknown":
        if projected_sector_pct > max_sector_exposure_pct:
            rejections.append(
                f"Sector '{new_sector}' exposure would be "
                f"{projected_sector_pct:.1%} "
                f"(limit {max_sector_exposure_pct:.0%})."
            )
        elif projected_sector_pct > max_sector_exposure_pct * 0.80:
            warnings.append(
                f"Sector '{new_sector}' exposure nearing limit: "
                f"{projected_sector_pct:.1%} of {max_sector_exposure_pct:.0%}."
            )

    # --- Check 3: Correlated positions ----------------------------------------
    projected_correlated = correlated_exposure + new_value
    projected_correlated_pct = projected_correlated / portfolio_value

    if projected_correlated_pct > max_correlated_pct:
        rejections.append(
            f"Correlated exposure ({new_sector}/{new_direction}) would be "
            f"{projected_correlated_pct:.1%} "
            f"(limit {max_correlated_pct:.0%})."
        )
    elif projected_correlated_pct > max_correlated_pct * 0.80:
        warnings.append(
            f"Correlated exposure ({new_sector}/{new_direction}) nearing "
            f"limit: {projected_correlated_pct:.1%} of "
            f"{max_correlated_pct:.0%}."
        )

    return {
        "approved": len(rejections) == 0,
        "warnings": warnings,
        "rejections": rejections,
        "current_exposure_pct": current_exposure_pct,
    }
