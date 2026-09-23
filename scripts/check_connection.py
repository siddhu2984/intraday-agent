"""Smoke test for the FYERS API: login, profile, funds, quotes, and 1-min history.

Usage:  python scripts/check_connection.py
Read-only — places no orders.
"""

from datetime import datetime, timedelta

from agent.broker.fyers_auth import IST, client

QUOTE_SYMBOLS = ["NSE:SBIN-EQ", "NSE:RELIANCE-EQ", "NSE:NIFTY50-INDEX"]
HISTORY_SYMBOL = "NSE:SBIN-EQ"
HISTORY_DAYS = 5


def check(name: str, response: dict) -> dict:
    status = "OK  " if response.get("s") == "ok" else "FAIL"
    print(f"[{status}] {name}")
    if status == "FAIL":
        print(f"       {response}")
    return response


def main() -> None:
    fyers = client()

    profile = check("profile", fyers.get_profile())
    if profile.get("s") == "ok":
        print(f"       logged in as {profile['data'].get('name')} ({profile['data'].get('fy_id')})")

    funds = check("funds", fyers.funds())
    for item in funds.get("fund_limit", []):
        if item.get("title") == "Available Balance":
            print(f"       available balance: ₹{item.get('equityAmount')}")

    quotes = check("quotes", fyers.quotes({"symbols": ",".join(QUOTE_SYMBOLS)}))
    for q in quotes.get("d", []):
        v = q.get("v", {})
        print(f"       {q.get('n'):<22} ltp={v.get('lp')}  chg%={v.get('chp')}  vol={v.get('volume')}")

    today = datetime.now(IST).date()
    history = check(
        f"1-min history {HISTORY_SYMBOL} (last {HISTORY_DAYS} days)",
        fyers.history({
            "symbol": HISTORY_SYMBOL,
            "resolution": "1",
            "date_format": "1",
            "range_from": (today - timedelta(days=HISTORY_DAYS)).isoformat(),
            "range_to": today.isoformat(),
            "cont_flag": "1",
        }),
    )
    candles = history.get("candles", [])
    if candles:
        first, last = candles[0], candles[-1]
        fmt = lambda c: datetime.fromtimestamp(c[0], IST).strftime("%Y-%m-%d %H:%M")  # noqa: E731
        print(f"       {len(candles)} candles, {fmt(first)} → {fmt(last)}")
        print(f"       last candle  O={last[1]} H={last[2]} L={last[3]} C={last[4]} V={last[5]}")


if __name__ == "__main__":
    main()
