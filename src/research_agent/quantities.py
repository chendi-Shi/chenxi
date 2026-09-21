"""Exact monetary-unit equivalence for English/Chinese news summaries."""

import re
from decimal import Decimal

from .validation import number_tokens

MONEY = re.compile(
    r"(?P<prefix>US\$|HK\$|USD\s*|HKD\s*|CNY\s*|RMB\s*|\$|[¥￥])?"
    r"(?P<number>[+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|million|thousand|万亿|千亿|十亿|百万|億|亿|萬|万|[kmbt])?\s*"
    r"(?P<suffix>美元|美金|港元|港币|人民币|元|dollars)?",
    re.IGNORECASE,
)
SCALE = {
    "": 1,
    "k": 1000,
    "thousand": 1000,
    "万": 10000,
    "萬": 10000,
    "m": 1_000_000,
    "million": 1_000_000,
    "百万": 1_000_000,
    "亿": 100_000_000,
    "億": 100_000_000,
    "b": 1_000_000_000,
    "billion": 1_000_000_000,
    "十亿": 1_000_000_000,
    "千亿": 100_000_000_000,
    "万亿": 1_000_000_000_000,
    "t": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}
CURRENCY = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "美元": "USD",
    "美金": "USD",
    "dollars": "USD",
    "hk$": "HKD",
    "hkd": "HKD",
    "港元": "HKD",
    "港币": "HKD",
    "cny": "CNY",
    "rmb": "CNY",
    "¥": "CNY",
    "￥": "CNY",
    "元": "CNY",
    "人民币": "CNY",
}


def monetary_tokens(text):
    amounts = set()

    def replace(match):
        prefix = (match["prefix"] or "").strip().lower()
        suffix = (match["suffix"] or "").lower()
        currency = CURRENCY.get(prefix or suffix)
        if not currency:
            return match[0]
        # Conflicting explicit currency markers are not accepted as a conversion.
        if prefix and suffix and CURRENCY.get(prefix) != CURRENCY.get(suffix):
            currency = "inconsistent_currency"
        amount = Decimal(match["number"].replace(",", "")) * SCALE[(match["scale"] or "").lower()]
        amounts.add((currency, amount))
        return " "

    remainder = MONEY.sub(replace, text.replace("−", "-"))
    return amounts, number_tokens(remainder)


def unsupported_numbers(summary, quote):
    summary_money, summary_other = monetary_tokens(summary)
    quote_money, quote_other = monetary_tokens(quote)
    return bool(summary_money - quote_money or summary_other - quote_other)
