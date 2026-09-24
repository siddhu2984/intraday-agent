"""Deterministic news groups for NSE filings, from config/news/categories.yaml.

Each NSE category (`desc`) maps to a group. A mapping written `text:<fallback>` means "the category says little —
look at the summary text": the keyword rules are tried in order and the first match wins; otherwise the fallback.
Categories not in the file get `default` (reported by the event study so the rules can be extended).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd
import yaml

from agent.broker.fyers_auth import PROJECT_ROOT

RULES_PATH = PROJECT_ROOT / "config" / "news" / "categories.yaml"


@dataclass(frozen=True)
class Classifier:
    categories: dict[str, str]                   # lower-cased NSE category → group or 'text:<fallback>'
    keywords: tuple[tuple[str, re.Pattern], ...]  # (group, pattern), in order
    default: str

    @classmethod
    def load(cls, path=RULES_PATH) -> Classifier:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls({k.strip().lower(): v for k, v in (data.get("categories") or {}).items()},
                   tuple((r["group"], re.compile(r["pattern"], re.I)) for r in data.get("keywords") or []),
                   data.get("default", "other"))

    def one(self, category: str, text: str) -> str:
        mapped = self.categories.get((category or "").strip().lower(), self.default)
        if not mapped.startswith("text:"):
            return mapped
        for group, pattern in self.keywords:
            if pattern.search(text or ""):
                return group
        return mapped.removeprefix("text:")

    def classify(self, categories: pd.Series, texts: pd.Series) -> pd.Series:
        return pd.Series([self.one(c, t) for c, t in zip(categories, texts)], index=categories.index, dtype=object)
