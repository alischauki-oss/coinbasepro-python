# Trading 212 portfolio checker (read-only)

Shows each holding's weight, profit/loss in your account currency, and the rise
needed to break even, then flags anything that breaks the rules in `rules.json`.
It **never places orders**.

## Setup
1. Trading 212 app -> Settings -> API (Beta) -> generate a key with **read-only**
   permissions (account data + portfolio). Keep the key and secret private.
2. `export T212_API_KEY=... T212_API_SECRET=...` (never commit these).
3. `python3 t212/portfolio_check.py`

Try it without a key: `python3 t212/portfolio_check.py --demo`.

## Rules (`rules.json`)
- `max_position_pct` – largest share of the portfolio any one stock may take.
- `loss_review_pct` – a holding down this much gets a "would you buy it today?" review.
- `tickers.<SYMBOL>.review_below / trim_above` – your own price levels, decided in
  advance, with the plan you committed to for each.

General information only, not financial advice.
