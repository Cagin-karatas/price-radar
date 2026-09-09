"""Faz 3 testleri: dışa aktarım, alarm değerlendirme, e-posta, pano."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from priceradar.alerts import TriggeredAlert, _in_cooldown, _matches, evaluate_alerts
from priceradar.config import Settings
from priceradar.db.models import Base, Offer, PriceAlert, PriceSnapshot, Product, Site
from priceradar.notifications.email import (
    EmailError,
    render_alert_email,
    send_alert_emails,
)
from priceradar.pipeline import PriceChange
from priceradar.reports.export import (
    build_export,
    build_workbook,
    offers_frame,
    products_frame,
    to_csv,
)


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/faz3.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(session):
    """İki sitede listelenen bir ürün, fiyat geçmişiyle."""
    site_a = Site(slug="shop-a", name="Shop A", base_url="https://a.example")
    site_b = Site(slug="shop-b", name="Shop B", base_url="https://b.example")
    product = Product(fingerprint="fp1", title="Sony WH-1000XM5", brand="Sony")
    session.add_all([site_a, site_b, product])
    await session.flush()

    offer_a = Offer(
        product_id=product.id, site_id=site_a.id, url="https://a.example/1",
        raw_title="Sony WH-1000XM5 Headphones", last_price=Decimal("349.99"),
        last_currency="USD", last_availability="in_stock",
        last_checked_at=datetime.now(timezone.utc),
    )
    offer_b = Offer(
        product_id=product.id, site_id=site_b.id, url="https://b.example/1",
        raw_title="SONY WH1000XM5", last_price=Decimal("319.00"),
        last_currency="USD", last_availability="in_stock",
        last_checked_at=datetime.now(timezone.utc),
    )
    session.add_all([offer_a, offer_b])
    await session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=3)
    session.add_all([
        PriceSnapshot(offer_id=offer_a.id, price=Decimal("399.99"), currency="USD",
                      availability="in_stock", captured_at=base),
        PriceSnapshot(offer_id=offer_a.id, price=Decimal("349.99"), currency="USD",
                      availability="in_stock", captured_at=base + timedelta(days=2)),
        PriceSnapshot(offer_id=offer_b.id, price=Decimal("319.00"), currency="USD",
                      availability="in_stock", captured_at=base + timedelta(days=1)),
    ])
    await session.commit()

    return {"session": session, "product": product, "offer_a": offer_a, "offer_b": offer_b}


# --- dışa aktarım -----------------------------------------------------------

async def test_products_frame_computes_spread(seeded):
    frame = await products_frame(seeded["session"])

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["teklif_sayisi"] == 2
    assert row["en_dusuk"] == Decimal("319.00")
    assert row["en_yuksek"] == Decimal("349.99")
    # Siteler arası fark: tablonun asıl değeri
    assert row["fark"] == Decimal("30.99")


async def test_offers_frame_can_filter_by_site(seeded):
    all_offers = await offers_frame(seeded["session"])
    filtered = await offers_frame(seeded["session"], site="shop-b")

    assert len(all_offers) == 2
    assert len(filtered) == 1
    assert filtered.iloc[0]["site"] == "shop-b"


async def test_csv_export_has_bom_for_excel(seeded):
    """BOM olmadan Excel Turkce karakterleri bozuk aciyor."""
    content, filename, media_type = await build_export(seeded["session"], "products", "csv")

    assert content.startswith(b"\xef\xbb\xbf")
    assert filename.endswith(".csv")
    assert "text/csv" in media_type


async def test_excel_export_opens_and_has_data(seeded):
    content, filename, media_type = await build_export(seeded["session"], "products", "xlsx")

    assert filename.endswith(".xlsx")
    frame = pd.read_excel(io.BytesIO(content))
    assert "urun" in frame.columns
    assert frame.iloc[0]["urun"] == "Sony WH-1000XM5"


async def test_workbook_has_three_sheets(seeded):
    content, _, _ = await build_workbook(seeded["session"])

    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
    assert set(sheets) == {"Urunler", "Teklifler", "Fiyat gecmisi"}
    assert not sheets["Fiyat gecmisi"].empty


async def test_history_export_handles_timezones(seeded):
    """Excel saat dilimli tarihleri kabul etmiyor; yazma asamasi patlamamali."""
    content, _, _ = await build_export(seeded["session"], "history", "xlsx")

    frame = pd.read_excel(io.BytesIO(content))
    assert len(frame) == 3


async def test_empty_export_produces_readable_file(session):
    """Veri yokken bos sayfa yerine aciklayici bir satir olmali."""
    content, _, _ = await build_export(session, "products", "xlsx")

    frame = pd.read_excel(io.BytesIO(content))
    assert "bilgi" in frame.columns


async def test_unknown_dataset_rejected(session):
    with pytest.raises(ValueError, match="Bilinmeyen veri kümesi"):
        await build_export(session, "uzay", "csv")


def test_to_csv_roundtrip():
    frame = pd.DataFrame({"ürün": ["Kalem"], "fiyat": [10.5]})
    parsed = pd.read_csv(io.BytesIO(to_csv(frame)))
    assert parsed.iloc[0]["ürün"] == "Kalem"


# --- alarm eşleştirme -------------------------------------------------------

def change(old: str | None, new: str | None, offer_id: int = 1) -> PriceChange:
    return PriceChange(
        offer_id=offer_id,
        product_title="Ürün",
        site_slug="shop-a",
        url="https://a.example/1",
        old_price=Decimal(old) if old else None,
        new_price=Decimal(new) if new else None,
        currency="USD",
    )


def test_target_price_triggers_below_threshold():
    alert = PriceAlert(product_id=1, email="a@b.com", target_price=Decimal("300"))

    assert _matches(alert, change("350", "299")) is not None
    assert _matches(alert, change("350", "301")) is None


def test_target_price_triggers_exactly_at_threshold():
    alert = PriceAlert(product_id=1, email="a@b.com", target_price=Decimal("300"))
    assert _matches(alert, change("350", "300")) is not None


def test_drop_percent_triggers():
    alert = PriceAlert(product_id=1, email="a@b.com", drop_percent=Decimal("20"))

    assert _matches(alert, change("100", "75")) is not None   # %25 düşüş
    assert _matches(alert, change("100", "85")) is None       # %15 düşüş


def test_price_increase_never_triggers():
    alert = PriceAlert(product_id=1, email="a@b.com", drop_percent=Decimal("10"))
    assert _matches(alert, change("100", "150")) is None


def test_missing_new_price_never_triggers():
    """Urun stoktan kalkip fiyati kaybolduysa bu bir firsat degil."""
    alert = PriceAlert(product_id=1, email="a@b.com", target_price=Decimal("300"))
    assert _matches(alert, change("350", None)) is None


def test_cooldown_blocks_recent_trigger():
    now = datetime.now(timezone.utc)
    recent = PriceAlert(
        product_id=1, email="a@b.com", target_price=Decimal("300"),
        last_triggered_at=now - timedelta(hours=2),
    )
    old = PriceAlert(
        product_id=1, email="a@b.com", target_price=Decimal("300"),
        last_triggered_at=now - timedelta(hours=20),
    )
    fresh = PriceAlert(product_id=1, email="a@b.com", target_price=Decimal("300"))

    assert _in_cooldown(recent, now, 12) is True
    assert _in_cooldown(old, now, 12) is False
    assert _in_cooldown(fresh, now, 12) is False


def test_cooldown_handles_naive_datetime():
    """SQLite tarihleri saat dilimsiz dondurebiliyor; karsilastirma patlamamali."""
    now = datetime.now(timezone.utc)
    alert = PriceAlert(
        product_id=1, email="a@b.com", target_price=Decimal("300"),
        last_triggered_at=(now - timedelta(hours=2)).replace(tzinfo=None),
    )
    assert _in_cooldown(alert, now, 12) is True


async def test_evaluate_alerts_triggers_and_marks(seeded):
    session = seeded["session"]
    session.add(PriceAlert(
        product_id=seeded["product"].id, email="ben@ornek.com", target_price=Decimal("330")
    ))
    await session.flush()

    triggered = await evaluate_alerts(
        session, [change("349.99", "299.00", offer_id=seeded["offer_a"].id)]
    )

    assert len(triggered) == 1
    assert triggered[0].email == "ben@ornek.com"
    assert "hedef fiyat" in triggered[0].reason

    alert = await session.scalar(select(PriceAlert))
    assert alert.last_triggered_at is not None


async def test_evaluate_alerts_respects_cooldown(seeded):
    session = seeded["session"]
    session.add(PriceAlert(
        product_id=seeded["product"].id, email="ben@ornek.com",
        target_price=Decimal("330"),
        last_triggered_at=datetime.now(timezone.utc) - timedelta(hours=1),
    ))
    await session.flush()

    triggered = await evaluate_alerts(
        session, [change("349.99", "299.00", offer_id=seeded["offer_a"].id)]
    )

    assert triggered == []


async def test_inactive_alerts_ignored(seeded):
    session = seeded["session"]
    session.add(PriceAlert(
        product_id=seeded["product"].id, email="ben@ornek.com",
        target_price=Decimal("330"), active=False,
    ))
    await session.flush()

    triggered = await evaluate_alerts(
        session, [change("349.99", "299.00", offer_id=seeded["offer_a"].id)]
    )

    assert triggered == []


async def test_no_drops_means_no_work(seeded):
    session = seeded["session"]
    session.add(PriceAlert(
        product_id=seeded["product"].id, email="ben@ornek.com", target_price=Decimal("330")
    ))
    await session.flush()

    # Fiyat arttı: düşüş yok
    triggered = await evaluate_alerts(
        session, [change("300", "400", offer_id=seeded["offer_a"].id)]
    )

    assert triggered == []


# --- e-posta ----------------------------------------------------------------

def sample_alert(**overrides) -> TriggeredAlert:
    data = {
        "alert_id": 1, "email": "ben@ornek.com", "product_id": 1,
        "product_title": "Sony WH-1000XM5", "site_slug": "shop-a",
        "url": "https://a.example/1", "old_price": Decimal("400"),
        "new_price": Decimal("300"), "currency": "USD", "percent": -25.0,
        "reason": "hedef fiyatın altına indi",
    }
    data.update(overrides)
    return TriggeredAlert(**data)


def test_single_alert_email_names_the_product():
    subject, text, html = render_alert_email([sample_alert()])

    assert "Sony WH-1000XM5" in subject
    assert "300" in text
    assert "<table" in html


def test_multiple_alerts_summarised_in_subject():
    subject, _, html = render_alert_email([sample_alert(), sample_alert(alert_id=2)])

    assert "2 üründe" in subject
    assert html.count("<tr>") >= 2


async def test_send_without_credentials_raises():
    settings = Settings(smtp_user="", smtp_password="")

    with pytest.raises(EmailError, match="kimlik bilgileri"):
        await send_alert_emails(settings, [sample_alert()])


async def test_send_groups_by_recipient(monkeypatch):
    """Bir kullanicinin bes urunu ucuzladiysa bes ayri e-posta gitmemeli."""
    settings = Settings(smtp_user="bot@ornek.com", smtp_password="gizli")
    captured = {}

    def fake_send(_settings, messages):
        captured["messages"] = messages

    monkeypatch.setattr("priceradar.notifications.email._send_sync", fake_send)

    result = await send_alert_emails(settings, [
        sample_alert(email="a@ornek.com"),
        sample_alert(email="a@ornek.com", product_title="İkinci ürün"),
        sample_alert(email="b@ornek.com"),
    ])

    assert result == {"sent": 2, "recipients": 2}
    assert len(captured["messages"]) == 2


async def test_send_with_no_alerts_does_nothing():
    result = await send_alert_emails(Settings(), [])
    assert result["sent"] == 0


# --- API uçları -------------------------------------------------------------

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("PRICERADAR_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/api3.db")
    monkeypatch.setenv("PRICERADAR_SITES_DIR", str(tmp_path / "yok"))
    monkeypatch.setenv("PRICERADAR_SCRAPE_INTERVAL_MINUTES", "0")

    from priceradar.api import deps
    from priceradar.api.main import create_app
    from priceradar.db import session as session_module

    deps.get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None

    app = create_app(deps.get_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        async with app.router.lifespan_context(app):
            yield http_client

    deps.get_settings.cache_clear()


async def test_dashboard_is_served(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert "price-radar" in response.text
    assert "<svg" in response.text or "priceChart" in response.text


async def test_export_endpoint_sets_download_header(client):
    response = await client.get("/export/products.csv")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


async def test_export_rejects_unknown_format(client):
    response = await client.get("/export/products.pdf")
    assert response.status_code == 422


async def test_alert_crud_flow(client):
    from priceradar.db.session import session_scope

    async with session_scope() as db:
        product = Product(fingerprint="fp-api", title="Test Ürün")
        db.add(product)
        await db.flush()
        product_id = product.id

    created = await client.post("/alerts", json={
        "product_id": product_id, "email": "ben@ornek.com", "target_price": "250.00",
    })
    assert created.status_code == 201
    alert_id = created.json()["id"]
    assert created.json()["product_title"] == "Test Ürün"

    duplicate = await client.post("/alerts", json={
        "product_id": product_id, "email": "ben@ornek.com", "target_price": "200.00",
    })
    assert duplicate.status_code == 409

    updated = await client.patch(f"/alerts/{alert_id}", json={"active": False})
    assert updated.json()["active"] is False

    listed = await client.get("/alerts?active=false")
    assert len(listed.json()) == 1

    assert (await client.delete(f"/alerts/{alert_id}")).status_code == 204
    assert (await client.get("/alerts")).json() == []


async def test_alert_requires_a_condition(client):
    """Iki kosul da bossa alarm hic tetiklenmez; kayitta reddet."""
    response = await client.post("/alerts", json={
        "product_id": 1, "email": "ben@ornek.com",
    })
    assert response.status_code == 422


async def test_alert_for_missing_product_returns_404(client):
    response = await client.post("/alerts", json={
        "product_id": 9999, "email": "ben@ornek.com", "target_price": "100",
    })
    assert response.status_code == 404


async def test_alert_preview_renders_email(client):
    from priceradar.db.session import session_scope

    async with session_scope() as db:
        product = Product(fingerprint="fp-prev", title="Önizleme Ürünü")
        db.add(product)
        await db.flush()
        product_id = product.id

    created = await client.post("/alerts", json={
        "product_id": product_id, "email": "ben@ornek.com", "drop_percent": "15",
    })
    alert_id = created.json()["id"]

    response = await client.get(f"/alerts/{alert_id}/preview")

    assert response.status_code == 200
    assert "Önizleme Ürünü" in response.json()["html"]


async def test_download_header_has_plain_filename(client):
    """curl -J yalnizca duz `filename=` okuyor; uzun bicim tek basina yetmiyor.

    Sadece filename*= gonderildiginde curl dosyayi URL'nin son parcasiyla
    ("workbook.xlsx") kaydediyordu.
    """
    response = await client.get("/export/products.csv")
    disposition = response.headers["content-disposition"]

    assert 'filename="priceradar-products' in disposition   # duz bicim
    assert "filename*=UTF-8''" in disposition                # uzun bicim
