# intraday-agent

Rule-based intraday trading agent for NSE cash equities (Opening Range Breakout), with a Claude-scored news veto.
Early stage: broker connectivity (FYERS) works; the rest is designed in `docs/`.

## Docs
- [Architecture & plan](docs/architecture.md)
- [Daily runbook](docs/daily-runbook.md)
- [Stock selection walkthrough](docs/stock-selection.md)

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # then fill in your FYERS app credentials
```

## Check the broker connection
```powershell
python scripts\check_connection.py
```
Opens the FYERS login once per day, then checks profile, funds, quotes and 1-min history. Read-only — places no orders.
