"""Build the point-in-time Nifty 500 membership (architecture.md §4.4, survivorship-free backtest universe).

Usage:  python scripts/build_nifty500_history.py
Needs `pdftotext` on PATH (ships with Git for Windows / poppler).

Inputs (config/universe/):
  nifty500_sources.txt         archived constituent snapshots + NSE Indices press releases to use
  nifty500_manual_changes.csv  changes from releases whose tables don't parse (revocations, exclusions without replacement)
  nifty500_name_aliases.csv    release company names that don't match any listed name (renames)
Downloads are cached in data/universe_sources/.

Method:
  1. Parse each release's "Nifty 500" section: numbered company rows under "being excluded" / "being included"
     and the effective date. Companies are matched by name (the PDF symbol column is unreliable across page breaks),
     using the snapshot closest in time, because names get reused (e.g. "Tata Motors Ltd." after the 2025 demerger).
  2. Start from the first snapshot, apply all changes in date order, and check the result equals every later
     snapshot. Any difference is an error — the output is only written when the replay reproduces all snapshots.
  3. Write, per stock, its membership intervals with the current FYERS symbol and industry.

Outputs (config/universe/):
  nifty500_changes.csv   one row per membership change
  nifty500_members.csv   one row per membership interval: member on day d if start <= d < end (end empty = still in)
"""

from __future__ import annotations

import datetime as dt
import gzip
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "universe"
CACHE = ROOT / "data" / "universe_sources"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

CURRENT_LIST_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
FYERS_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
RELEASE_URL = "https://www.niftyindices.com/Press_Release/{}.pdf"
WAYBACK_URL = "https://web.archive.org/web/{}id_/{}"


# ---------------------------------------------------------------------------------------------------------- fetch

def download(url: str, path: Path, retries: int = 3) -> Path:
    if path.exists() and path.stat().st_size:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            if data[:2] == b"\x1f\x8b":  # the archive stores some captures gzip-compressed
                data = gzip.decompress(data)
            path.write_bytes(data)
            time.sleep(0.5)  # be polite to NSE and the Internet Archive
            return path
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f"download failed: {url}: {exc}") from exc
            time.sleep(10 * attempt)


def read_list(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    return df.apply(lambda s: s.str.strip() if s.dtype == object else s)


def release_text(release: str) -> str:
    txt = CACHE / "releases" / f"{release}.txt"
    if not txt.exists():
        pdf = download(RELEASE_URL.format(release), CACHE / "releases" / f"{release}.pdf")
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
    return txt.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------------------------------------- parse

HEADER = re.compile(r"^\s*(?:[a-z]|\d+)\)\s*Nifty 500(?:\s{2,}\S+)?\s*$")
SECTION_END = re.compile(r"^\s*(?:[a-z]\)|\d+\)|[A-Z]\.)\s+\S")
EFFECTIVE = re.compile(r"effective\s+from\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})")
ROW = re.compile(r"^\s*(\d{1,3})\s+([A-Za-z0-9].*?)\s*$")
GLUED_SYMBOL = re.compile(r"^(.*?(?:ltd\.?|limited|trust|reit))\s{2,}\S+$", re.I)


def parse_release(release: str) -> list[dict]:
    """Nifty 500 exclusions/inclusions in one press release, by company name."""
    text = release_text(release)
    date_match = EFFECTIVE.search(text)
    if not date_match:
        raise ValueError(f"{release}: no effective date found")
    effective = dt.datetime.strptime(re.sub(r"\s+", " ", date_match.group(1)), "%B %d, %Y").date()
    lines = text.splitlines()
    changes = []
    for start in (i for i, line in enumerate(lines) if HEADER.match(line)):
        mode = None
        for line in lines[start + 1:]:
            if SECTION_END.match(line):
                break
            lowered = line.lower()
            if "being excluded" in lowered:
                mode = "exclude"
                continue
            if "being included" in lowered:
                mode = "include"
                continue
            row = ROW.match(line)
            if mode and row:
                company = GLUED_SYMBOL.sub(r"\1", row.group(2)).rstrip("*#").strip()
                changes.append({"effective": effective, "change": mode, "row": int(row.group(1)),
                                "company": company, "source": release})
    for mode in ("exclude", "include"):  # rows are numbered 1..n: a gap means the layout lost a row
        numbers = sorted(c["row"] for c in changes if c["change"] == mode)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"{release}: {mode} rows not numbered 1..n: {numbers}")
    return changes


# ---------------------------------------------------------------------------------------------------------- identity

def normalize(name: str) -> str:
    name = name.lower().replace("&", " and ")
    name = re.sub(r"\(india\)|\bindia\b|\blimited\b|\bltd\b|\bcompany\b|\bco\b|\bcorporation\b|\bcorp\b|\bthe\b", " ", name)
    return re.sub(r"[^a-z0-9]", "", name)


class Identity:
    """Links ISINs and symbols that belong to one stock: a split changes the ISIN, a rename the symbol."""

    def __init__(self, pairs):
        self.parent: dict[str, str] = {}
        for symbol, isin in pairs:
            self._union("S:" + symbol, "I:" + isin)

    def _find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def _union(self, a: str, b: str) -> None:
        self.parent[self._find(a)] = self._find(b)

    def of_isin(self, isin: str) -> str:
        return self._find("I:" + isin)

    def of_symbol(self, symbol: str) -> str:
        return self._find("S:" + symbol)


# ---------------------------------------------------------------------------------------------------------- build

def main() -> int:
    if not shutil.which("pdftotext"):
        sys.exit("pdftotext not found on PATH (install poppler, or run from Git Bash)")

    sources = [line.split() for line in (CONFIG / "nifty500_sources.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    today = dt.date.today()

    # Snapshots: archived lists + today's list. DUMMY* rows are NSE placeholders during demergers (not tradable).
    snapshots: dict[dt.date, pd.DataFrame] = {}
    for _, stamp, url in (s for s in sources if s[0] == "snapshot"):
        path = download(WAYBACK_URL.format(stamp, url), CACHE / "snapshots" / f"{stamp[:8]}.csv")
        snapshots[dt.datetime.strptime(stamp[:8], "%Y%m%d").date()] = read_list(path)
    snapshots[today] = read_list(download(CURRENT_LIST_URL, CACHE / "snapshots" / f"{today:%Y%m%d}.csv"))
    snapshots = {d: df[~df["Symbol"].str.startswith("DUMMY")] for d, df in sorted(snapshots.items())}

    equity = read_list(download(EQUITY_LIST_URL, CACHE / f"EQUITY_L_{today:%Y%m%d}.csv"))
    fyers = pd.read_csv(download(FYERS_MASTER_URL, CACHE / f"fyers_NSE_CM_{today:%Y%m%d}.csv"), header=None)
    # col 5: ISIN, col 9: FYERS ticker. Stocks moved to trade-for-trade trade as -BE/-BZ; their history under that
    # ticker also covers their -EQ years. Prefer -EQ when both exist.
    fyers = fyers[fyers[9].str.match(r"NSE:.+-(EQ|BE|BZ)$", na=False)]
    fyers = fyers.assign(eq=~fyers[9].str.endswith("-EQ")).sort_values("eq").drop_duplicates(5)

    all_rows = pd.concat(snapshots.values())
    identity = Identity(list(zip(all_rows["Symbol"], all_rows["ISIN Code"]))
                        + list(zip(equity["SYMBOL"], equity["ISIN NUMBER"])))
    for snap_date, df in snapshots.items():
        ids = df["ISIN Code"].map(identity.of_isin)
        if ids.duplicated().any():
            sys.exit(f"identity conflict in snapshot {snap_date}: {df[ids.duplicated(keep=False)]['Symbol'].tolist()}")

    # Name lookup, time-aware: an inclusion names a company as listed after the change, an exclusion before it.
    names_by_date = {d: {normalize(n): i for n, i in zip(df["Company Name"], df["ISIN Code"])}
                     for d, df in snapshots.items()}
    current_names = {normalize(n): i for n, i in zip(equity["NAME OF COMPANY"], equity["ISIN NUMBER"])}
    aliases = {normalize(r.company): r.symbol for r in pd.read_csv(CONFIG / "nifty500_name_aliases.csv").itertuples()}

    def resolve(company: str, effective: dt.date, change: str) -> str | None:
        key = normalize(company)
        if key in aliases:
            return identity.of_symbol(aliases[key])
        after = [d for d in names_by_date if d >= effective]
        before = [d for d in names_by_date if d < effective][::-1]
        for d in (after + before if change == "include" else before + after):
            if key in names_by_date[d]:
                return identity.of_isin(names_by_date[d][key])
        return identity.of_isin(current_names[key]) if key in current_names else None

    # Changes from releases, then manual changes (revocations cancel the announced change).
    changes = pd.DataFrame([c for _, release in (s for s in sources if s[0] == "release")
                            for c in (parse_release(release) if release not in manual_only(CONFIG) else [])])
    changes["stock"] = [resolve(r.company, r.effective, r.change) for r in changes.itertuples()]
    unresolved = changes[changes["stock"].isna()]
    future = unresolved["effective"] > today
    if (~future).any():
        sys.exit(f"unresolved companies:\n{unresolved[~future].to_string()}")
    if future.any():
        print(f"note: {future.sum()} companies in changes effective after today are not listed yet — skipped")
    changes = changes[changes["stock"].notna()].copy()

    manual = pd.read_csv(CONFIG / "nifty500_manual_changes.csv", parse_dates=["effective"])
    added = []
    for m in manual.itertuples():
        stock, effective = identity.of_symbol(m.symbol), m.effective.date()
        if m.change.startswith("revoke_"):
            revoked = (changes["stock"] == stock) & (changes["effective"] == effective) & (changes["change"] == m.change[7:])
            if revoked.sum() != 1:
                sys.exit(f"manual change {m.symbol} {m.change}: expected 1 announced change to revoke, found {revoked.sum()}")
            changes = changes[~revoked]
        else:
            added.append({"effective": effective, "change": m.change, "row": 0,
                          "company": m.symbol, "source": m.source, "stock": stock})
    changes = pd.concat([changes, pd.DataFrame(added)], ignore_index=True)
    changes = changes.sort_values(["effective", "change"], ignore_index=True)  # 'exclude' before 'include'

    # Replay from the first snapshot and verify against all later ones.
    dates = list(snapshots)
    members = {identity.of_isin(i) for i in snapshots[dates[0]]["ISIN Code"]}
    intervals = {stock: [[dates[0], None]] for stock in members}
    applied, failures = 0, []
    for snap_date in dates[1:]:
        due = changes.iloc[applied:]
        due = due[due["effective"] <= snap_date]
        for c in due.itertuples():
            if c.change == "include":
                if c.stock in members:
                    failures.append(f"{c.effective} {c.source}: {c.company} included but already a member")
                members.add(c.stock)
                intervals.setdefault(c.stock, []).append([c.effective, None])
            else:
                if c.stock not in members:
                    failures.append(f"{c.effective} {c.source}: {c.company} excluded but not a member")
                members.discard(c.stock)
                intervals[c.stock][-1][1] = c.effective
        applied += len(due)
        expected = {identity.of_isin(i) for i in snapshots[snap_date]["ISIN Code"]}
        if members != expected:
            failures.append(f"snapshot {snap_date}: replay has {len(members)}, snapshot {len(expected)}; "
                            f"only in replay {label(identity, members - expected)}, "
                            f"only in snapshot {label(identity, expected - members)}")
        else:
            print(f"   {snap_date}: replay matches snapshot ({len(expected)} stocks)")
    pending = changes.iloc[applied:]
    if failures:
        print("\nVERIFICATION FAILED:\n   " + "\n   ".join(failures))
        return 1

    # Output: symbol/ISIN/company/industry from the latest snapshot or listing that has the stock.
    info = {}
    for snap_date, df in snapshots.items():
        for r in df.itertuples():
            info[identity.of_isin(r._5)] = {"symbol": r.Symbol, "isin": r._5, "company": r._1, "industry": r.Industry}
    for r in equity.itertuples():
        info.setdefault(identity.of_isin(r._7), {"symbol": r.SYMBOL, "isin": r._7, "company": r._2, "industry": ""})
    fyers_by_stock = {identity.of_isin(isin): ticker for isin, ticker in zip(fyers[5], fyers[9])}

    rows = []
    for stock, spans in intervals.items():
        for start, end in spans:
            rows.append({**info.get(stock, {"symbol": label(identity, {stock})}),
                         "fyers_symbol": fyers_by_stock.get(stock, ""), "start": start, "end": end or ""})
    out = pd.DataFrame(rows).sort_values(["symbol", "start"], ignore_index=True)
    out.to_csv(CONFIG / "nifty500_members.csv", index=False, lineterminator="\n")

    changes["symbol"] = [info.get(s, {}).get("symbol", "") for s in changes["stock"]]
    changes[["effective", "change", "symbol", "company", "source"]].to_csv(
        CONFIG / "nifty500_changes.csv", index=False, lineterminator="\n")

    in_window = out[(out["end"] == "") | (out["end"].astype(str) > str(dates[0]))]
    print(f"\nwrote {len(out)} membership intervals for {out['isin'].nunique()} stocks; "
          f"{len(changes)} changes ({len(pending)} effective after the last snapshot)")
    print(f"   no FYERS symbol today (delisted/merged): {sorted(in_window[in_window['fyers_symbol'] == '']['symbol'])}")
    print(f"   no industry: {sorted(out[out['industry'].fillna('') == '']['symbol'])}")
    return 0


def manual_only(config: Path) -> set[str]:
    """Releases whose Nifty 500 changes come only from the manual file (their tables don't parse)."""
    return set(pd.read_csv(config / "nifty500_manual_changes.csv")["source"])


def label(identity: Identity, stocks) -> list[str]:
    return sorted("/".join(sorted(k[2:] for k in identity.parent if k.startswith("S:") and identity._find(k) == s))
                  for s in stocks)


if __name__ == "__main__":
    sys.exit(main())
