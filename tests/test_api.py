"""REST API testleri.

Gerçek ASGI uygulaması, gerçek veritabanı (geçici SQLite), gerçek lifespan.
Sadece ağa çıkan kazıma sahteleniyor — hedef sitelere bağımlı test yazmak
CI'ı kırılgan yapar.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from priceradar.db.models import Offer, Product, Site
from priceradar.db.session import session_scope


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Temiz veritabanı + zamanlayıcısı kapalı uygulama."""
    monkeypatch.setenv("PRICERADAR_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("PRICERADAR_SITES_DIR", str(tmp_path / "bos-siteler"))
    monkeypatch.setenv("PRICERADAR_SCRAPE_INTERVAL_MINUTES", "0")

    from priceradar.api import deps
    from priceradar.api.main import create_app
    from priceradar.db import session as session_module

    deps.get_settings.cache_clear()
    # Modül seviyesindeki motor testler arasında sızmasın
    session_module._engine = None
    session_module._session_factory = None

    app = create_app(deps.get_settings())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        async with app.router.lifespan_context(app):
            yield http_client

    deps.get_settings.cache_clear()


def site_payload(slug: str = "test-shop", **overrides) -> dict:
    payload = {
        "slug": slug,
        "name": "Test Shop",
        "base_url": "https://test.example",
        "adapter": "css",
        "currency": "USD",
        "start_urls": ["https://test.example/urunler"],
        "selectors": {"item": "div.card", "title": "a@title", "price": "span.price"},
    }
    payload.update(overrides)
    return payload


# --- sağlık -----------------------------------------------------------------

async def test_health_reports_ok(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["scheduler_running"] is False  # interval=0


async def test_openapi_schema_builds(client):
    """Sema uretilemiyorsa bir yerde tip hatasi vardir."""
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/sites" in paths
    assert "/products" in paths
    assert "/scrape" in paths


# --- site CRUD --------------------------------------------------------------

async def test_create_and_get_site(client):
    created = await client.post("/sites", json=site_payload())
    assert created.status_code == 201

    body = created.json()
    assert body["slug"] == "test-shop"
    assert body["managed_by"] == "api"
    assert body["enabled"] is True

    fetched = await client.get("/sites/test-shop")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Test Shop"


async def test_duplicate_slug_conflicts(client):
    await client.post("/sites", json=site_payload())
    duplicate = await client.post("/sites", json=site_payload())

    assert duplicate.status_code == 409


async def test_site_requires_item_selector(client):
    """selectors.item olmadan adaptor hicbir sey bulamaz; hatayi kayitta ver."""
    response = await client.post(
        "/sites", json=site_payload(selectors={"title": "h3"})
    )

    assert response.status_code == 422
    assert "selectors.item" in response.text


async def test_invalid_slug_rejected(client):
    response = await client.post("/sites", json=site_payload(slug="Büyük Harf!"))
    assert response.status_code == 422


async def test_unknown_adapter_rejected(client):
    response = await client.post("/sites", json=site_payload(adapter="uzaydan"))

    assert response.status_code == 422
    assert "Bilinmeyen adaptor" in response.text


async def test_partial_update(client):
    await client.post("/sites", json=site_payload())

    response = await client.patch("/sites/test-shop", json={"enabled": False})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["name"] == "Test Shop"  # dokunulmayan alan korundu


async def test_update_missing_site_returns_404(client):
    response = await client.patch("/sites/yok", json={"enabled": False})
    assert response.status_code == 404


async def test_delete_site_without_offers(client):
    await client.post("/sites", json=site_payload())

    response = await client.delete("/sites/test-shop")
    assert response.status_code == 204

    assert (await client.get("/sites/test-shop")).status_code == 404


async def test_delete_site_with_offers_requires_force(client):
    """Fiyat gecmisini kazayla silmek kolay olmamali."""
    await client.post("/sites", json=site_payload())

    async with session_scope() as session:
        site = (await session.execute(Site.__table__.select())).first()
        product = Product(fingerprint="abc123", title="Ürün")
        session.add(product)
        await session.flush()
        session.add(
            Offer(
                product_id=product.id,
                site_id=site.id,
                url="https://test.example/1",
                raw_title="Ürün",
                last_price=Decimal("10.00"),
            )
        )

    blocked = await client.delete("/sites/test-shop")
    assert blocked.status_code == 409
    assert "force=true" in blocked.text

    forced = await client.delete("/sites/test-shop?force=true")
    assert forced.status_code == 204


async def test_list_sites_filters_by_enabled(client):
    await client.post("/sites", json=site_payload("acik"))
    await client.post("/sites", json=site_payload("kapali", enabled=False))

    enabled = await client.get("/sites?enabled=true")
    disabled = await client.get("/sites?enabled=false")

    assert [s["slug"] for s in enabled.json()] == ["acik"]
    assert [s["slug"] for s in disabled.json()] == ["kapali"]


# --- ürünler ----------------------------------------------------------------

@pytest_asyncio.fixture
async def seeded(client):
    """İki sitede listelenen bir ürün + tek sitede olan bir ürün."""
    await client.post("/sites", json=site_payload("shop-a"))
    await client.post("/sites", json=site_payload("shop-b"))

    async with session_scope() as session:
        sites = {
            s.slug: s
            for s in (await session.execute(Site.__table__.select())).mappings().all()
        }

        shared = Product(fingerprint="fp-shared", title="Sony WH-1000XM5", brand="Sony")
        solo = Product(fingerprint="fp-solo", title="Logitech MX Master 3S")
        session.add_all([shared, solo])
        await session.flush()

        session.add_all([
            Offer(
                product_id=shared.id,
                site_id=sites["shop-a"]["id"],
                url="https://a.example/xm5",
                raw_title="Sony WH-1000XM5 Headphones",
                last_price=Decimal("349.99"),
                last_currency="USD",
                last_availability="in_stock",
            ),
            Offer(
                product_id=shared.id,
                site_id=sites["shop-b"]["id"],
                url="https://b.example/xm5",
                raw_title="SONY WH1000XM5 Kulaklık",
                last_price=Decimal("319.00"),
                last_currency="USD",
                last_availability="in_stock",
            ),
            Offer(
                product_id=solo.id,
                site_id=sites["shop-a"]["id"],
                url="https://a.example/mx",
                raw_title="Logitech MX Master 3S",
                last_price=Decimal("99.00"),
                last_currency="USD",
                last_availability="out_of_stock",
            ),
        ])

    return client


async def test_list_products_shows_price_range(seeded):
    response = await seeded.get("/products")

    assert response.status_code == 200
    products = {p["title"]: p for p in response.json()}

    shared = products["Sony WH-1000XM5"]
    assert shared["offer_count"] == 2
    assert Decimal(shared["min_price"]) == Decimal("319.00")
    assert Decimal(shared["max_price"]) == Decimal("349.99")


async def test_min_offers_filter_finds_comparable_products(seeded):
    """Fiyat karsilastirmasi sadece birden fazla sitede olan urunlerde anlamli."""
    response = await seeded.get("/products?min_offers=2")

    titles = [p["title"] for p in response.json()]
    assert titles == ["Sony WH-1000XM5"]


async def test_product_search(seeded):
    response = await seeded.get("/products?search=logitech")
    assert [p["title"] for p in response.json()] == ["Logitech MX Master 3S"]


async def test_product_detail_lists_offers(seeded):
    listed = (await seeded.get("/products")).json()
    product_id = next(p["id"] for p in listed if p["offer_count"] == 2)

    response = await seeded.get(f"/products/{product_id}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["offers"]) == 2
    assert {o["site_slug"] for o in body["offers"]} == {"shop-a", "shop-b"}


async def test_missing_product_returns_404(seeded):
    assert (await seeded.get("/products/9999")).status_code == 404


async def test_offers_filter_by_availability(seeded):
    response = await seeded.get("/offers?availability=out_of_stock")

    body = response.json()
    assert len(body) == 1
    assert body[0]["raw_title"] == "Logitech MX Master 3S"


async def test_offers_filter_by_site(seeded):
    response = await seeded.get("/offers?site=shop-b")

    assert len(response.json()) == 1


# --- işler ------------------------------------------------------------------

async def test_scrape_returns_202_and_runs_in_background(client, monkeypatch):
    """Kazima dakikalar surebilir; istek beklemeden donmeli."""
    await client.post("/sites", json=site_payload())

    called = {}

    async def fake_run(slugs=None):
        called["slugs"] = slugs
        return {"status": "ok"}

    from priceradar.api import main as main_module

    monkeypatch.setattr(main_module, "logger", main_module.logger)

    # Uygulamanın kendi manager'ını sahtele
    app = client._transport.app
    app.state.scrape_manager.run_scrape = fake_run

    response = await client.post("/scrape", json={"slugs": ["test-shop"]})

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert called["slugs"] == ["test-shop"]


async def test_scrape_unknown_site_returns_404(client):
    response = await client.post("/scrape", json={"slugs": ["olmayan-site"]})

    assert response.status_code == 404
    assert "olmayan-site" in response.text


async def test_scrape_conflicts_while_running(client):
    app = client._transport.app
    app.state.scrape_manager._running = True

    try:
        response = await client.post("/scrape", json={})
        assert response.status_code == 409
    finally:
        app.state.scrape_manager._running = False


async def test_scheduler_status_reports_disabled(client):
    response = await client.get("/scheduler")

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["scrape_interval_minutes"] == 0
    assert body["jobs"] == []


async def test_runs_endpoint_empty_initially(client):
    response = await client.get("/runs")

    assert response.status_code == 200
    assert response.json() == []


async def test_missing_run_returns_404(client):
    assert (await client.get("/runs/123")).status_code == 404
