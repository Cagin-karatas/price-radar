"""Ham metinden fiyat, para birimi ve stok durumu çıkarma.

Scraping'in en sinsi kısmı burası. "1.299,99 TL" ile "1,299.99 USD" aynı
karakterleri farklı anlamlarda kullanıyor; ondalık ayracını yanlış okursan
1299 lira 1,29 lira olur ve bunu fark etmek haftalar sürer.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum

CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₺": "TRY",
    "TL": "TRY",
    "¥": "JPY",
    "₹": "INR",
}

CURRENCY_CODES = {"USD", "EUR", "GBP", "TRY", "JPY", "INR", "CHF", "CAD", "AUD"}

# Rakam grubunu yakalar: 1.299,99 / 1,299.99 / 1299.99 / 1299
NUMBER_RE = re.compile(r"\d[\d\s.,\u00a0']*\d|\d")


class Availability(str, Enum):
    """Stok durumu."""

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


IN_STOCK_PATTERNS = [
    r"\bin stock\b", r"\bavailable\b", r"\bstokta\b", r"\bmevcut\b",
    r"\badd to (cart|basket)\b", r"\bsepete ekle\b", r"\bbuy now\b",
]
OUT_OF_STOCK_PATTERNS = [
    r"\bout of stock\b", r"\bsold out\b", r"\bunavailable\b",
    r"\bstokta yok\b", r"\btükendi\b", r"\btukendi\b", r"\bcurrently unavailable\b",
]
PREORDER_PATTERNS = [r"\bpre-?order\b", r"\bön ?sipariş\b", r"\bon siparis\b"]


def detect_currency(text: str, default: str | None = None) -> str | None:
    """Metinden para birimi kodu çıkarır."""
    if not text:
        return default

    upper = text.upper()

    # Önce 3 harfli kodlar: "1299 USD"
    for code in CURRENCY_CODES:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            return code

    # Sonra semboller. Uzun olanı önce dene ("US$" > "$")
    for symbol in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol.upper() in upper:
            return CURRENCY_SYMBOLS[symbol]

    return default


def parse_decimal(raw: str) -> Decimal | None:
    """Sayı metnini Decimal'e çevirir, ondalık ayracını tahmin ederek.

    Kural: son ayraçtan sonra 1-2 hane varsa o ondalık ayracıdır;
    tam 3 hane varsa binlik ayracıdır.

        "1.299,99" -> 1299.99   (virgül ondalık)
        "1,299.99" -> 1299.99   (nokta ondalık)
        "1.299"    -> 1299      (nokta binlik)
        "1.29"     -> 1.29      (nokta ondalık)
    """
    if not raw:
        return None

    cleaned = re.sub(r"[\s\u00a0']", "", raw)
    if not cleaned:
        return None

    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    separator_index = max(last_comma, last_dot)

    if separator_index == -1:
        digits = cleaned
    else:
        decimals = len(cleaned) - separator_index - 1
        if decimals in (1, 2):
            # Ondalık ayracı: sağdaki kesir, soldaki gruplama
            integer_part = re.sub(r"[.,]", "", cleaned[:separator_index])
            digits = f"{integer_part}.{cleaned[separator_index + 1:]}"
        else:
            # 3 hane veya daha fazla: binlik ayracı
            digits = re.sub(r"[.,]", "", cleaned)

    digits = re.sub(r"[^\d.\-]", "", digits)
    if not digits or digits in {"-", "."}:
        return None

    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def parse_price(text: str, default_currency: str | None = None) -> tuple[Decimal | None, str | None]:
    """Serbest metinden (fiyat, para birimi) çıkarır.

    "£51.77"          -> (51.77, "GBP")
    "1.299,99 TL"     -> (1299.99, "TRY")
    "Price: $1,299"   -> (1299, "USD")
    """
    if not text:
        return None, default_currency

    currency = detect_currency(text, default_currency)

    match = NUMBER_RE.search(text)
    if not match:
        return None, currency

    value = parse_decimal(match.group(0))
    if value is None or value < 0:
        return None, currency

    return value, currency


def parse_availability(text: str) -> Availability:
    """Stok metnini sınıflandırır.

    Sıra önemli: "out of stock" içinde "stock" geçiyor, önce olumsuzu kontrol et.
    """
    if not text:
        return Availability.UNKNOWN

    lowered = text.lower()

    for pattern in OUT_OF_STOCK_PATTERNS:
        if re.search(pattern, lowered):
            return Availability.OUT_OF_STOCK

    for pattern in PREORDER_PATTERNS:
        if re.search(pattern, lowered):
            return Availability.PREORDER

    for pattern in IN_STOCK_PATTERNS:
        if re.search(pattern, lowered):
            return Availability.IN_STOCK

    # "23 available" gibi sayı içeren ifadeler
    if re.search(r"\b\d+\s*(adet|piece|item|left|kaldı)\b", lowered):
        return Availability.IN_STOCK

    return Availability.UNKNOWN


def clean_text(value: str | None) -> str:
    """Boşlukları normalleştirir, görünmez karakterleri atar."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()
