from datetime import date, datetime

import pandas as pd
import pytest

from agent.broker.fyers_auth import IST
from agent.news.classify import Classifier
from agent.news.nse import months, parse, parse_rss
from agent.news.store import NewsStore, enrich


def api_row(seq, symbol="HFCL", isin="INE548A01028", desc="Updates", text="HFCL Limited bags Order worth Rs 100 cr",
            an="15-Mar-2024 10:00:00", dis="15-Mar-2024 10:00:05", pdf=None, name="HFCL Limited"):
    return {"seq_id": seq, "symbol": symbol, "sm_isin": isin, "sm_name": name, "desc": desc,
            "attchmntText": text, "attchmntFile": pdf or f"https://x/{seq}.pdf", "an_dt": an, "exchdisstime": dis,
            "smIndustry": "Telecom"}


RSS = b"""<rss><channel>
<item><title>HFCL Limited</title><link>https://x/7.pdf</link>
<description>HFCL Limited has informed the Exchange about an order |SUBJECT: General Updates</description>
<pubDate>15-Mar-2024 10:00:06</pubDate></item>
</channel></rss>"""


# --- client parsing ---

def test_parse_uses_exchange_time_and_falls_back_to_company_time():
    df = parse([api_row("1"), api_row("2", dis=None), api_row("2")])  # duplicate seq_id kept once
    assert list(df["seq_id"]) == ["1", "2"]
    assert df.loc[0, "public_ts"] == pd.Timestamp(datetime(2024, 3, 15, 10, 0, 5, tzinfo=IST))
    assert df.loc[1, "public_ts"] == pd.Timestamp(datetime(2024, 3, 15, 10, 0, 5, tzinfo=IST))  # last duplicate wins
    fallback = parse([api_row("3", dis="-")])
    assert fallback.loc[0, "public_ts"] == fallback.loc[0, "company_ts"]


def test_parse_rss():
    df = parse_rss(RSS)
    r = df.iloc[0]
    assert r["seq_id"].startswith("rss:") and r["source"] == "rss" and r["symbol"] == ""
    assert (r["company"], r["category"], r["pdf_url"]) == ("HFCL Limited", "General Updates", "https://x/7.pdf")
    assert r["public_ts"].hour == 10


def test_months():
    assert months(date(2024, 1, 20), date(2024, 3, 5)) == [
        (date(2024, 1, 20), date(2024, 1, 31)), (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 5))]


# --- classifier ---

@pytest.mark.parametrize("category, text, group", [
    ("Financial Result Updates", "", "results"),
    ("Bagging/Receiving of orders/contracts", "", "order_win"),
    ("Updates", "Receipt of Letter of Award from MPRDC", "order_win"),
    ("Updates", "2 orders received from Additional Commissioner of State Tax (Appeals)", "regulatory"),
    ("Updates", "Disclosure w.r.t. order passed by GST Authority", "regulatory"),
    ("Press Release", "titled \"Zydus receives final approval from USFDA\"", "usfda"),
    ("Updates", "Transcript of Conference Call on the Un-Audited Financial Results", "noise"),
    ("Updates", "Production, Sales and Export figures for the month of May 2026", "business_update"),
    ("Updates", "Intimation under Regulation 30", "other"),
    ("Press Release", "Company wins award for sustainability", "press_release"),
    ("Loss of Share Certificates", "", "noise"),
    ("Some New Category", "", "other"),
])
def test_classifier(category, text, group):
    assert Classifier.load().one(category, text) == group


# --- store ---

def test_upsert_dedupes_and_returns_only_new(tmp_path):
    store = NewsStore(tmp_path)
    assert len(store.upsert(parse([api_row("1"), api_row("2")]))) == 2
    new = store.upsert(parse([api_row("2"), api_row("3")]))
    assert list(new["seq_id"]) == ["3"]
    assert len(store.read_raw(date(2024, 3, 1), date(2024, 3, 31))) == 3


def test_api_row_replaces_the_rss_row_of_the_same_filing(tmp_path):
    store = NewsStore(tmp_path)
    assert len(store.upsert(parse_rss(RSS))) == 1                  # seen first via RSS
    new = store.upsert(parse([api_row("7", pdf="https://x/7.pdf")]))
    assert new.empty                                               # not news any more
    raw = store.read_raw(date(2024, 3, 1), date(2024, 3, 31))
    assert list(raw["source"]) == ["api"]
    assert store.upsert(parse_rss(RSS)).empty                      # and RSS doesn't bring it back


def members_frame():
    return pd.DataFrame({"symbol": ["HFCL", "ETERNAL"], "isin": ["INE548A01028", "INE758T01015"],
                         "company": ["HFCL Ltd.", "Eternal Ltd."], "industry": ["Telecom", "Consumer Services"],
                         "fyers_symbol": ["NSE:HFCL-EQ", "NSE:ETERNAL-EQ"],
                         "start": [date(2023, 1, 1), date(2024, 6, 1)], "end": [None, None]})


def test_enrich_matches_by_isin_symbol_or_company_and_membership_is_point_in_time():
    df = pd.concat([parse([api_row("1"),                                           # ISIN
                           api_row("2", symbol="ZOMATO", isin="INE758T01015"),     # renamed, ISIN matches
                           api_row("3", symbol="OTHER", isin="X", name="Other Co Ltd")]),               # not a member
                    parse_rss(RSS)], ignore_index=True)                            # company name only
    out = enrich(df, members_frame(), Classifier.load(), members_only=False).set_index("seq_id")
    assert out.loc["1", "fyers_symbol"] == "NSE:HFCL-EQ" and out.loc["1", "member"]
    assert out.loc["2", "fyers_symbol"] == "NSE:ETERNAL-EQ" and not out.loc["2", "member"]  # joined in June
    assert pd.isna(out.loc["3", "fyers_symbol"])
    rss = out[out["source"] == "rss"].iloc[0]
    assert rss["fyers_symbol"] == "NSE:HFCL-EQ" and rss["group"] == "other"
    assert out.loc["1", "group"] == "order_win" and out.loc["1", "sector"] == "Telecom"
