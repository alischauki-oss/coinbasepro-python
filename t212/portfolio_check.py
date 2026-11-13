#!/usr/bin/env python3
"""Read-only Trading 212 portfolio checker.

Pulls your positions and cash from the Trading 212 public API, shows each
holding's weight and profit/loss in your account currency, and flags anything
that breaks the rules in rules.json. It never places orders.

Usage:
    python3 t212/portfolio_check.py            # live account (needs API key)
    python3 t212/portfolio_check.py --demo     # sample data, no key needed
    python3 t212/portfolio_check.py --json     # machine-readable output

Credentials (create in the T212 app: Settings -> API (Beta), read-only):
    T212_API_KEY, T212_API_SECRET   -> sent as HTTP Basic auth
    T212_API_KEY only               -> sent as the legacy Authorization header
    T212_ENV=demo                   -> use the practice account instead of live
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "rules.json")

# Sample data matching the COIN position from the app screenshot.
DEMO = {
    "currency": "GBP",
    "cash_free": 0.0,
    "positions": [
        {
            "ticker": "COIN_US_EQ",
            "quantity": 42.37771805,
            "avg_price": 164.84,
            "price": 126.91,
            "cost": 5227.38,
            "value": 4061.28,
        }
    ],
}


class T212Client:
    def __init__(self, env="live", key=None, secret=None, timeout=20):
        self.base = "https://%s.trading212.com/api/v0" % env
        self.key = key
        self.secret = secret
        self.timeout = timeout

    def _headers(self):
        h = {"Accept": "application/json", "User-Agent": "portfolio-check/1.0"}
        if self.key and self.secret:
            token = base64.b64encode(("%s:%s" % (self.key, self.secret)).encode())
            h["Authorization"] = "Basic " + token.decode()
        elif self.key:
            h["Authorization"] = self.key
        return h

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise RuntimeError("GET %s -> HTTP %s: %s" % (path, e.code, body))

    def snapshot(self):
        """Return {currency, cash_free, positions[]} in a normalised shape."""
        info = self._try("/equity/account/info") or {}
        cash = self._try("/equity/account/cash") or {}
        currency = info.get("currencyCode") or "GBP"

        positions = self._from_positions_endpoint()
        if positions is None:
            positions = self._from_portfolio_endpoint(cash)
        return {
            "currency": currency,
            "cash_free": float(cash.get("free") or 0.0),
            "positions": positions,
        }

    def _try(self, path):
        try:
            return self.get(path)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def _from_positions_endpoint(self):
        """Newer endpoint that reports cost/value in the account currency."""
        data = self._try("/equity/positions")
        if not isinstance(data, list) or not data or "walletImpact" not in data[0]:
            return None if not isinstance(data, list) or data else []
        out = []
        for p in data:
            inst = p.get("instrument") or {}
            w = p["walletImpact"]
            out.append({
                "ticker": inst.get("ticker") or p.get("ticker"),
                "quantity": float(p["quantity"]),
                "avg_price": float(p.get("averagePricePaid") or p.get("averagePrice") or 0),
                "price": float(p["currentPrice"]),
                "cost": float(w["totalCost"]),
                "value": float(w["currentValue"]),
            })
        return out

    def _from_portfolio_endpoint(self, cash):
        """Older endpoint: prices in instrument currency, P/L in account currency.

        Account-currency value is estimated by splitting the account's total
        invested value across positions in proportion to their market value.
        """
        data = self.get("/equity/portfolio")
        raw = [(p, float(p["quantity"]) * float(p["currentPrice"])) for p in data]
        total_native = sum(v for _, v in raw) or 1.0
        invested_value = float(cash.get("invested") or 0) + float(cash.get("ppl") or 0)
        out = []
        for p, native in raw:
            value = invested_value * native / total_native if invested_value else native
            ppl = float(p.get("ppl") or 0)
            out.append({
                "ticker": p["ticker"],
                "quantity": float(p["quantity"]),
                "avg_price": float(p["averagePrice"]),
                "price": float(p["currentPrice"]),
                "cost": value - ppl,
                "value": value,
            })
        return out


def short_ticker(t):
    return t.split("_")[0] if t else "?"


def analyse(snap, rules):
    positions = snap["positions"]
    invested = sum(p["value"] for p in positions)
    total = invested + snap["cash_free"]
    max_pct = rules.get("max_position_pct", 20)
    loss_review = rules.get("loss_review_pct", 20)
    per_ticker = rules.get("tickers", {})

    rows, alerts = [], []
    for p in sorted(positions, key=lambda x: -x["value"]):
        sym = short_ticker(p["ticker"])
        weight = 100 * p["value"] / total if total else 0
        pl = p["value"] - p["cost"]
        pl_pct = 100 * pl / p["cost"] if p["cost"] else 0
        breakeven = 100 * (p["avg_price"] / p["price"] - 1) if p["price"] else 0
        rows.append(dict(p, symbol=sym, weight=weight, pl=pl, pl_pct=pl_pct,
                         to_breakeven_pct=breakeven))

        if weight > max_pct:
            target_value = total * max_pct / 100
            alerts.append("%s is %.0f%% of your portfolio (limit %s%%). Trimming "
                          "about %s%.0f would bring it back to the limit."
                          % (sym, weight, max_pct, cur_sym(snap), p["value"] - target_value))
        if pl_pct <= -loss_review:
            alerts.append("%s is down %.1f%%, past your %s%% review line. Re-check: "
                          "would you buy it today at %.2f?" % (sym, -pl_pct, loss_review, p["price"]))

        t = per_ticker.get(sym, {})
        if t.get("review_below") and p["price"] < t["review_below"]:
            alerts.append("%s at %.2f is below your review level %.2f: %s"
                          % (sym, p["price"], t["review_below"],
                             t.get("plan_below", "act on your plan.")))
        if t.get("trim_above") and p["price"] > t["trim_above"]:
            alerts.append("%s at %.2f is above your trim level %.2f: %s"
                          % (sym, p["price"], t["trim_above"],
                             t.get("plan_above", "consider trimming back to target.")))

    return {"currency": snap["currency"], "total": total, "invested": invested,
            "cash_free": snap["cash_free"], "positions": rows, "alerts": alerts}


def cur_sym(snap_or_report):
    return {"GBP": "£", "USD": "$", "EUR": "€"}.get(snap_or_report["currency"], "")


def print_report(rep):
    c = cur_sym(rep)
    print("Portfolio: %s%.2f  (invested %s%.2f, free cash %s%.2f)"
          % (c, rep["total"], c, rep["invested"], c, rep["cash_free"]))
    print()
    print("%-8s %8s %10s %10s %11s %11s %8s %10s"
          % ("Ticker", "Weight", "Avg", "Price", "Value", "P/L", "P/L%", "BreakEven"))
    for r in rep["positions"]:
        print("%-8s %7.1f%% %10.2f %10.2f %11s %11s %7.1f%% %+9.1f%%"
              % (r["symbol"], r["weight"], r["avg_price"], r["price"],
                 "%s%.2f" % (c, r["value"]), "%s%.2f" % (c, r["pl"]),
                 r["pl_pct"], r["to_breakeven_pct"]))
    print()
    if rep["alerts"]:
        print("Rule alerts:")
        for a in rep["alerts"]:
            print("  - " + a)
    else:
        print("No rule alerts.")
    print("\nAvg/Price are in each stock's own currency. BreakEven = rise needed "
          "to get back to your average price.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true", help="use sample data")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="path to rules.json")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)

    with open(args.rules) as f:
        rules = json.load(f)

    if args.demo:
        snap = DEMO
    else:
        client = T212Client(env=os.environ.get("T212_ENV", "live"),
                            key=os.environ.get("T212_API_KEY"),
                            secret=os.environ.get("T212_API_SECRET"))
        try:
            snap = client.snapshot()
        except (RuntimeError, urllib.error.URLError) as e:
            sys.exit("Could not read Trading 212: %s" % e)

    rep = analyse(snap, rules)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)


if __name__ == "__main__":
    main()
