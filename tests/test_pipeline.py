"""Boru hattı ve veritabanı entegrasyon testleri.

Gerçek bir SQLite veritabanı kullanılıyor (bellekte değil, geçici dosyada):
async motor ve gerçek SQL çalışıyor, sadece sunucu PostgreSQL yerine SQLite.
Şema aynı olduğu için bu testler üretim davranışını temsil ediyor.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from priceradar.db.models import Base, Offer, PriceSnapshot, Product, Site
from priceradar.db.session import normalize_url
from priceradar.normalize import Availability
from priceradar.pipeline import ingest_items, run_collection, scrape_sites
from priceradar.scraping.base import Adapter, ScrapedItem, SiteConfig, register

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture
async def session(tmp_path):
    """Her test için temiz bir veritabanı."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


def site_config(slug: str = "testshop") -> SiteConfig:
    return SiteConfig.from_dict({
        "slug": slug,
        "name": "Test Shop",
        "base_url": "https://test.example",
        "currency": "USD",
    })


def item(
    title: str,
    price: str | None,
    *,
    slug: str = "testshop",
    url: str | None = None,
    availability: Availability = Availability.IN_STOCK,
    brand: str | None = None,
) -> ScrapedItem:
    return ScrapedItem(
        site_slug=slug,
        url=url or f"https://test.example/{title.lower().replace(' ', '-')}",
        title=title,
        price=Decimal(price) if price is not None else None,
        currency="USD",
        availability=availability,
        brand=brand,
    )


# --- temel işleme -----------------------------------------------------------

async def test_first_run_creates_products_and_offers(session):
    items = [item("Sony WH-1000XM5 Headphones", "349.99"), item("Logitech MX Master 3S", "99.00")]

    result = await ingest_items(session, items, {"testshop": site_config()})
    await session.commit()

    assert result.items_found == 2
    assert result.products_created == 2
    assert result.offers_created == 2
    assert await session.scalar(select(func.count(Product.id))) == 2
    assert await session.scalar(select(func.count(Site.id))) == 1

    # İlk görüşte de anlık görüntü yazılır: geçmişin başlangıcı
    assert await session.scalar(select(func.count(PriceSnapshot.id))) == 2


async def test_unchanged_price_writes_no_snapshot(session):
    """Ayni fiyat tekrar gorulurse gecmise satir eklenmemeli.

    Saatte bir calisan sistemde 1000 urun icin gunde 24.000 gereksiz satir
    demek olurdu.
    """
    items = [item("Sony WH-1000XM5 Headphones", "349.99")]
    configs = {"testshop": site_config()}

    await ingest_items(session, items, configs)
    await session.commit()

    result = await ingest_items(session, items, configs)
    await session.commit()

    assert result.products_created == 0
    assert result.offers_created == 0
    assert len(result.price_changes) == 0
    assert await session.scalar(select(func.count(PriceSnapshot.id))) == 1


async def test_price_change_is_recorded(session):
    configs = {"testshop": site_config()}

    await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "349.99")], configs)
    await session.commit()

    result = await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "299.99")], configs)
    await session.commit()

    assert len(result.price_changes) == 1
    change = result.price_changes[0]
    assert change.old_price == Decimal("349.99")
    assert change.new_price == Decimal("299.99")
    assert change.is_drop
    assert change.percent == pytest.approx(-14.29, abs=0.01)

    assert await session.scalar(select(func.count(PriceSnapshot.id))) == 2

    offer = await session.scalar(select(Offer))
    assert offer.last_price == Decimal("299.99")


async def test_price_increase_is_not_a_drop(session):
    configs = {"testshop": site_config()}

    await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "299.99")], configs)
    await session.commit()

    result = await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "349.99")], configs)
    await session.commit()

    assert len(result.price_changes) == 1
    assert result.price_changes[0].is_drop is False
    assert result.drops == []


async def test_availability_change_creates_snapshot(session):
    configs = {"testshop": site_config()}

    await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "349.99")], configs)
    await session.commit()

    result = await ingest_items(
        session,
        [item("Sony WH-1000XM5 Headphones", "349.99", availability=Availability.OUT_OF_STOCK)],
        configs,
    )
    await session.commit()

    assert result.availability_changes == 1
    assert len(result.price_changes) == 0  # fiyat degismedi
    assert await session.scalar(select(func.count(PriceSnapshot.id))) == 2


async def test_price_becoming_none_counts_as_change(session):
    """Urun stoktan kalkinca fiyat kaybolur; bu da bir degisimdir."""
    configs = {"testshop": site_config()}

    await ingest_items(session, [item("Sony WH-1000XM5 Headphones", "349.99")], configs)
    await session.commit()

    result = await ingest_items(
        session,
        [item("Sony WH-1000XM5 Headphones", None, availability=Availability.OUT_OF_STOCK)],
        configs,
    )
    await session.commit()

    assert len(result.price_changes) == 1
    assert result.price_changes[0].new_price is None


# --- siteler arası ürün eşleştirme -----------------------------------------

async def test_same_product_across_sites_shares_one_product(session):
    """Projenin asil degeri burada: iki sitedeki ayni urun tek Product olmali."""
    configs = {"shop-a": site_config("shop-a"), "shop-b": site_config("shop-b")}

    items = [
        item(
            "Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
            "349.99",
            slug="shop-a",
            url="https://a.example/sony-xm5",
        ),
        item(
            "SONY WH1000XM5 Kablosuz Kulaklık",
            "10499.00",
            slug="shop-b",
            url="https://b.example/sony",
        ),
    ]

    result = await ingest_items(session, items, configs)
    await session.commit()

    assert result.products_created == 1     # tek urun
    assert result.offers_created == 2       # iki teklif

    product = await session.scalar(select(Product))
    offers = (await session.execute(select(Offer).where(Offer.product_id == product.id))).scalars().all()
    assert len(offers) == 2


async def test_different_variants_stay_separate(session):
    configs = {"testshop": site_config()}

    items = [
        item("Apple iPhone 15 128GB", "999.00", url="https://test.example/i15-128"),
        item("Apple iPhone 15 256GB", "1199.00", url="https://test.example/i15-256"),
    ]

    result = await ingest_items(session, items, configs)
    await session.commit()

    assert result.products_created == 2
    assert result.duplicates_merged == 0


async def test_duplicate_within_same_site_is_merged(session):
    """Ayni site farkli URL'lerde ayni urunu listeleyebilir."""
    configs = {"testshop": site_config()}

    items = [
        item("Logitech MX Master 3S Wireless Mouse", "99.00", url="https://test.example/a"),
        item("Logitech MX Master 3S Kablosuz Mouse", "97.50", url="https://test.example/b"),
    ]

    result = await ingest_items(session, items, configs)
    await session.commit()

    assert result.products_created == 1
    assert result.offers_created == 2
    assert result.duplicates_merged == 1


async def test_same_url_twice_creates_one_offer(session):
    configs = {"testshop": site_config()}
    listing = item("Sony WH-1000XM5 Headphones", "349.99")

    result = await ingest_items(session, [listing, listing], configs)
    await session.commit()

    assert result.offers_created == 1


async def test_unknown_site_is_skipped(session):
    result = await ingest_items(session, [item("Ürün", "10.00", slug="tanimsiz")], {})
    await session.commit()

    assert result.offers_created == 0
    assert await session.scalar(select(func.count(Offer.id))) == 0


# --- uçtan uca kazıma -------------------------------------------------------

class FakeClient:
    """URL'ye göre fixture döndüren sahte istemci."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch_text(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        for fragment, payload in self.mapping.items():
            if fragment in url:
                return payload
        raise AssertionError(f"Beklenmeyen URL: {url}")


def books_config(max_pages: int = 2) -> SiteConfig:
    return SiteConfig.from_dict({
        "slug": "books",
        "name": "Books to Scrape",
        "base_url": "https://books.toscrape.com",
        "adapter": "css",
        "currency": "GBP",
        "max_pages": max_pages,
        "start_urls": ["https://books.toscrape.com/catalogue/page-1.html"],
        "selectors": {
            "item": "article.product_pod",
            "title": "h3 a@title",
            "url": "h3 a@href",
            "price": "p.price_color",
            "availability": "p.instock",
        },
        "pagination": {"next": "li.next a"},
    })


async def test_scrape_follows_pagination():
    client = FakeClient({
        "page-1.html": (FIXTURES / "books_page1.html").read_text(encoding="utf-8"),
        "page-2.html": (FIXTURES / "books_page2.html").read_text(encoding="utf-8"),
    })

    items, errors, per_site = await scrape_sites([books_config()], client)

    assert not errors
    assert per_site["books"] == 4          # 3 + 1
    assert len(client.calls) == 2
    assert any(i.title == "A Murder in Time" for i in items)


async def test_max_pages_limits_crawl():
    client = FakeClient({
        "page-1.html": (FIXTURES / "books_page1.html").read_text(encoding="utf-8"),
        "page-2.html": (FIXTURES / "books_page2.html").read_text(encoding="utf-8"),
    })

    items, _, _ = await scrape_sites([books_config(max_pages=1)], client)

    assert len(client.calls) == 1
    assert len(items) == 3


async def test_broken_site_does_not_stop_others():
    """Bir site cokerse digerleri toplanmaya devam etmeli."""

    @register
    class ExplodingAdapter(Adapter):
        name = "exploding"

        async def scrape(self):
            raise RuntimeError("site çöktü")

    broken = SiteConfig.from_dict({
        "slug": "broken",
        "name": "Broken",
        "base_url": "https://broken.example",
        "adapter": "exploding",
    })

    client = FakeClient({"page-1.html": (FIXTURES / "books_page1.html").read_text(encoding="utf-8")})

    items, errors, per_site = await scrape_sites([books_config(max_pages=1), broken], client)

    assert "broken" in errors
    assert "site çöktü" in errors["broken"]
    assert per_site["books"] == 3     # digeri calisti
    assert len(items) == 3


async def test_run_collection_records_run(session):
    client = FakeClient({
        "page-1.html": (FIXTURES / "books_page1.html").read_text(encoding="utf-8"),
    })

    result, run = await run_collection(session, [books_config(max_pages=1)], client)
    await session.commit()

    assert result.items_found == 3
    assert run.items_found == 3
    assert run.products_created == 3
    assert run.finished_at is not None
    assert run.errors is None


# --- yardımcılar ------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("postgresql://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("postgres://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("sqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("sqlite+aiosqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("postgresql+asyncpg://u@h/d", "postgresql+asyncpg://u@h/d"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


# --- şema doğrulama (canlı çalıştırmada bulunan hata) -----------------------

async def test_init_db_creates_schema_on_empty_database(tmp_path):
    """Sifir yapilandirmayla ilk calistirma: tablolar olusturulmali."""
    from sqlalchemy import inspect

    from priceradar.db import session as session_module
    from priceradar.db.session import dispose_db, init_db

    session_module._engine = None
    session_module._session_factory = None

    engine = await init_db(f"sqlite+aiosqlite:///{tmp_path}/yeni.db")

    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))

    assert {"sites", "products", "offers", "price_snapshots"} <= tables
    await dispose_db()


async def test_init_db_rejects_outdated_schema(tmp_path):
    """Eski semali veritabani sessizce kabul edilmemeli.

    create_all mevcut tablolara dokunmuyor; Alembic eklendikten sonra bu,
    uygulamanin ilk sorguda 'no such column' ile cokmesine yol aciyordu.
    """
    import sqlite3

    from priceradar.db import session as session_module
    from priceradar.db.session import SchemaOutdatedError, dispose_db, init_db

    path = tmp_path / "eski.db"
    connection = sqlite3.connect(path)
    # Faz 1'deki sites tablosu: yeni yapilandirma sutunlari yok
    connection.execute(
        "CREATE TABLE sites (id INTEGER PRIMARY KEY, slug VARCHAR(64), "
        "name VARCHAR(200), base_url VARCHAR(500), adapter VARCHAR(64), "
        "enabled BOOLEAN, created_at DATETIME)"
    )
    connection.commit()
    connection.close()

    session_module._engine = None
    session_module._session_factory = None

    with pytest.raises(SchemaOutdatedError) as error:
        await init_db(f"sqlite+aiosqlite:///{path}")

    message = str(error.value)
    assert "currency" in message           # eksik sutunu adiyla soyler
    assert "alembic upgrade head" in message   # ne yapilacagini soyler

    await dispose_db()
