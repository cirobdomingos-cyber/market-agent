"""
Backtest module — time-machine simulation of MarketCoach recommendations.

Replays the intelligence pipeline against historical data to show what would
have happened if you followed MarketCoach's advice N days ago.

Interview angle:
  Backtesting is the bread and butter of quantitative finance. The key concept
  here is "no look-ahead bias" — at each decision point, the system only sees
  data that was available on that date. This is the same discipline used in
  ML train/test splits: you never train on future data.

  The alternative (Approach 1 — tracking actual past recommendations) is
  simpler but only works once you have historical recommendations. This
  approach (Approach 2 — time-machine) works from day one and lets you
  test different configurations retroactively.
"""
