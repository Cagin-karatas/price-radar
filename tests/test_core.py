"""Birim testleri: normalleştirme, eşleştirme, adaptör ayrıştırma, istemci."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from priceradar.matching import (
    extract_model_code,
    fingerprint,
    find_match,
    match,
    normalize_title,
    title_similarity,
    tokenize,
)
from priceradar.normalize import (
    Availability,
    detect_currency,
    parse_availability,
    parse_decimal,
    parse_price,
)
from priceradar.scraping.base import ScrapedItem, SiteConfig
from priceradar.scraping.client import DomainRateLimiter, ProxyPool
from priceradar.scraping.css_adapter import CssAdapter, extract

FIXTURES = Path(__file__).parent / "fixtures"


# --- sayı ve fiyat ayrıştırma ----------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.299,99", Decimal("1299.99")),   # TR/AB: virgül ondalık
        ("1,299.99", Decimal("1299.99")),   # ABD: nokta ondalık
        ("1.299", Decimal("1299")),         # 3 hane = binlik ayracı
        ("1.29", Decimal("1.29")),          # 2 hane = ondalık
        ("1 299,50", Decimal("1299.50")),   # boşluklu binlik
        ("47.82", Decimal("47.82")),
        ("999", Decimal("999")),
        ("10.499,00", Decimal("10499.00")),
        ("", None),
    ],
)
def test_parse_decimal(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "text,value,currency",
    [
        ("£51.77", Decimal("51.77"), "GBP"),
        ("10.499,00 TL", Decimal("10499.00"), "TRY"),
        ("$1,299.99", Decimal("1299.99"), "USD"),
        ("Fiyat: 3.250,50 ₺", Decimal("3250.50"), "TRY"),
        ("1299 USD", Decimal("1299"), "USD"),
        ("€89,90", Decimal("89.90"), "EUR"),
    ],
)
def test_parse_price(text, value, currency):
    parsed_value, parsed_currency = parse_price(text)
    assert parsed_value == value
    assert parsed_currency == currency


def test_parse_price_uses_default_currency():
    value, currency = parse_price("47.82", default_currency="GBP")
    assert value == Decimal("47.82")
    assert currency == "GBP"


def test_parse_price_returns_none_for_garbage():
    assert parse_price("Fiyat bulunamadı")[0] is None


def test_detect_currency_prefers_longer_symbol():
    assert detect_currency("US$ 50") == "USD"


# --- stok durumu ------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("In stock", Availability.IN_STOCK),
        ("Out of stock", Availability.OUT_OF_STOCK),
        ("Stokta", Availability.IN_STOCK),
        ("Tükendi", Availability.OUT_OF_STOCK),
        ("Pre-order", Availability.PREORDER),
        ("23 available", Availability.IN_STOCK),
        ("", Availability.UNKNOWN),
        ("Belirsiz metin", Availability.UNKNOWN),
    ],
)
def test_parse_availability(text, expected):
    assert parse_availability(text) == expected


def test_out_of_stock_wins_over_in_stock():
    """'out of stock' icinde 'stock' geciyor; olumsuz once kontrol edilmeli."""
    assert parse_availability("Currently out of stock") == Availability.OUT_OF_STOCK


# --- ürün eşleştirme --------------------------------------------------------

def test_normalize_title_handles_turkish():
    assert normalize_title("Kablosuz Kulaklık ŞARJLI") == "kablosuz kulaklik sarjli"


def test_tokenize_drops_stopwords():
    tokens = tokenize("Sony WH-1000XM5 Wireless Headphones with Free Shipping")
    assert "free" not in tokens
    assert "shipping" not in tokens
    assert "sony" in tokens


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Sony WH-1000XM5 Wireless", "wh1000xm5"),
        ("SONY WH1000XM5 Kablosuz", "wh1000xm5"),
        ("Logitech MX Master 3S", None),          # model kodu tek belirtec degil
        ("Samsung SM-G991B Galaxy", "smg991b"),
        ("Basit Kitap", None),
    ],
)
def test_extract_model_code(title, expected):
    assert extract_model_code(title) == expected


def test_same_product_different_titles_match():
    """Ayni urun farkli sitelerde farkli basliklarla listelenir."""
    result = match(
        "Sony WH-1000XM5 Wireless Noise Cancelling Headphones - Black",
        "SONY WH1000XM5 Kablosuz Kulaklık Siyah",
    )
    assert result.matched
    assert "model kodu" in result.reason


def test_different_generations_do_not_match():
    """iPhone 13 ile iPhone 14 baslik olarak cok benzer ama farkli urunler."""
    result = match("Apple iPhone 13 128GB Blue", "Apple iPhone 14 128GB Blue")
    assert result.matched is False


def test_different_brands_do_not_match():
    result = match(
        "Wireless Mouse 3S", "Wireless Mouse 3S", brand_a="Logitech", brand_b="Razer"
    )
    assert result.matched is False
    assert result.reason == "marka farklı"


def test_fuzzy_match_without_model_code():
    result = match(
        "Logitech MX Master 3S Wireless Mouse",
        "Logitech MX Master 3S Kablosuz Mouse",
    )
    assert result.matched
    assert result.score > 0.7


def test_fingerprint_is_word_order_independent():
    a = fingerprint("Kablosuz Sony Kulaklık")
    b = fingerprint("Sony Kulaklık Kablosuz")
    assert a == b


def test_fingerprint_prefers_model_code():
    a = fingerprint("Sony WH-1000XM5 Headphones Black")
    b = fingerprint("Sony WH1000XM5 Kulaklık")
    assert a == b


def test_fingerprint_differs_for_different_products():
    assert fingerprint("iPhone 13") != fingerprint("iPhone 14")


def test_title_similarity_bounds():
    assert title_similarity("aynı başlık", "aynı başlık") == pytest.approx(1.0)
    assert title_similarity("kulaklık", "") == 0.0


def test_find_match_returns_best_candidate():
    candidates = [
        (1, "Logitech MX Master 2S Mouse"),
        (2, "Logitech MX Master 3S Wireless Mouse"),
        (3, "Apple Magic Mouse"),
    ]
    found = find_match("Logitech MX Master 3S Kablosuz Mouse", candidates)
    assert found is not None
    assert found[0] == 2


def test_find_match_returns_none_when_nothing_close():
    assert find_match("Sony Kulaklık", [(1, "Bahçe Hortumu 20 Metre")]) is None


# --- CSS adaptörü -----------------------------------------------------------

def _books_config(**overrides) -> SiteConfig:
    data = {
        "slug": "books",
        "name": "Books",
        "base_url": "https://books.toscrape.com",
        "currency": "GBP",
        "start_urls": ["https://books.toscrape.com/catalogue/category/books/mystery_3/index.html"],
        "selectors": {
            "item": "article.product_pod",
            "title": "h3 a@title",
            "url": "h3 a@href",
            "price": "p.price_color",
            "availability": "p.instock",
            "image": "div.image_container img@src",
        },
        "pagination": {"next": "li.next a"},
    }
    data.update(overrides)
    return SiteConfig.from_dict(data)


def test_extract_reads_text_and_attribute():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup('<div><a href="/x" title="Başlık">Metin</a></div>', "lxml")
    assert extract(soup, "a") == "Metin"
    assert extract(soup, "a@href") == "/x"
    assert extract(soup, "a@title") == "Başlık"
    assert extract(soup, "span") == ""
    assert extract(soup, None) == ""


def test_css_adapter_parses_products():
    adapter = CssAdapter(_books_config(), client=None)
    html = (FIXTURES / "books_page1.html").read_text(encoding="utf-8")

    items = adapter.parse(html, "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html")

    assert len(items) == 3
    first = items[0]
    assert first.title == "Sharp Objects"
    assert first.price == Decimal("47.82")
    assert first.currency == "GBP"
    assert first.availability == Availability.IN_STOCK
    assert first.url.startswith("https://books.toscrape.com/")
    assert "sharp-objects" in first.url


def test_css_adapter_detects_out_of_stock():
    adapter = CssAdapter(_books_config(), client=None)
    html = (FIXTURES / "books_page1.html").read_text(encoding="utf-8")

    items = adapter.parse(html, "https://books.toscrape.com/x/")
    assert items[2].availability == Availability.OUT_OF_STOCK


def test_css_adapter_resolves_relative_urls():
    adapter = CssAdapter(_books_config(), client=None)
    html = (FIXTURES / "books_page1.html").read_text(encoding="utf-8")

    items = adapter.parse(html, "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html")
    # ../../../ tirmanisi dogru cozulmeli
    assert items[0].url == "https://books.toscrape.com/catalogue/sharp-objects_997/index.html"


def test_css_adapter_handles_turkish_prices():
    config = SiteConfig.from_dict({
        "slug": "shop-tr",
        "name": "TR Shop",
        "base_url": "https://example.com.tr",
        "currency": "TRY",
        "selectors": {
            "item": "div.card",
            "title": "a.title@title",
            "url": "a.title@href",
            "price": "span.price",
            "availability": "p.stock",
        },
    })
    adapter = CssAdapter(config, client=None)
    html = (FIXTURES / "shop_tr.html").read_text(encoding="utf-8")

    items = adapter.parse(html, "https://example.com.tr/liste")

    assert len(items) == 3
    assert items[0].price == Decimal("10499.00")
    assert items[0].currency == "TRY"
    assert items[1].price == Decimal("54999.90")
    assert items[1].availability == Availability.OUT_OF_STOCK


def test_css_adapter_raises_without_item_selector():
    config = _books_config(selectors={"title": "h3"})
    adapter = CssAdapter(config, client=None)

    with pytest.raises(ValueError, match="selectors.item"):
        adapter.parse("<html></html>", "https://example.com")


def test_scraped_item_validity():
    valid = ScrapedItem(site_slug="s", url="https://x.com/1", title="Ürün")
    invalid_title = ScrapedItem(site_slug="s", url="https://x.com/1", title="  ")
    invalid_url = ScrapedItem(site_slug="s", url="", title="Ürün")

    assert valid.is_valid
    assert not invalid_title.is_valid
    assert not invalid_url.is_valid


# --- istemci bileşenleri ----------------------------------------------------

def test_rate_limiter_waits_for_same_host():
    limiter = DomainRateLimiter(min_interval=0.2, jitter=0)

    async def scenario():
        first = await limiter.acquire("https://a.com/1")
        second = await limiter.acquire("https://a.com/2")
        other = await limiter.acquire("https://b.com/1")
        return first, second, other

    first, second, other = asyncio.run(scenario())

    assert first == 0.0        # ilk istek beklemez
    assert second > 0.1        # ayni siteye ikinci istek bekler
    assert other == 0.0        # farkli site beklemez


def test_proxy_pool_rotates():
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])

    assert pool.enabled
    first, second, third = pool.next(), pool.next(), pool.next()
    assert {first, second} == {"http://p1:8080", "http://p2:8080"}
    assert third == first  # dongusel


def test_proxy_pool_skips_failed():
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    pool.mark_failed("http://p1:8080")

    assert pool.next() == "http://p2:8080"


def test_empty_proxy_pool_is_disabled():
    pool = ProxyPool()
    assert not pool.enabled
    assert pool.next() is None


# --- sayısal varyant çelişkisi (bulanık eşleştirmenin güvenlik kilidi) ------

@pytest.mark.parametrize(
    "title,expected",
    [
        ("Apple iPhone 13 128GB", {"13", "128gb"}),
        ("Logitech MX Master 3S", {"3s"}),
        ("Sony Kulaklık", set()),
        ("Anker 65W Şarj Aleti", {"65w"}),
    ],
)
def test_spec_tokens(title, expected):
    from priceradar.matching import spec_tokens

    assert spec_tokens(title) == expected


@pytest.mark.parametrize(
    "a,b,conflict",
    [
        ("iPhone 13 128GB", "iPhone 14 128GB", True),    # 13 ≠ 14
        ("iPhone 13 128GB", "iPhone 13 256GB", True),    # kapasite farkli
        ("MX Master 3S Wireless", "MX Master 3S Kablosuz", False),
        ("Kitap 2020 Baskı", "Kitap", False),            # tek tarafli fazlalik
        ("Sony Kulaklık", "Sony Kulaklık", False),
    ],
)
def test_has_spec_conflict(a, b, conflict):
    from priceradar.matching import has_spec_conflict

    assert has_spec_conflict(a, b) is conflict


def test_storage_capacity_variants_stay_separate():
    """256GB ve 512GB ayri urunlerdir; ayni sayfada yan yana listelenirler."""
    result = match("MacBook Air M2 256GB", "MacBook Air M2 512GB")
    assert result.matched is False
    assert "varyant" in result.reason


def test_cross_language_titles_match():
    """Ayni urun TR ve EN sitelerde farkli dillerde listelenir."""
    result = match(
        "Logitech MX Master 3S Wireless Mouse",
        "Logitech MX Master 3S Kablosuz Mouse",
    )
    assert result.matched
