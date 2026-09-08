"""Fiyat alarmı değerlendirme.

Bir alarm iki şekilde tanımlanabilir:
  - **Hedef fiyat** — "300 TL'nin altına inerse haber ver"
  - **Yüzde düşüş** — "%15 ucuzlarsa haber ver"

İkisi birden verilirse ikisinden biri sağlandığında tetiklenir.

Soğuma süresi (cooldown) önemli: fiyat eşiğin hemen altında salınıyorsa
her kazımada e-posta gitmesin. Varsayılan 12 saat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Offer, PriceAlert, Product
from .pipeline import PriceChange

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_HOURS = 12


@dataclass
class TriggeredAlert:
    """Tetiklenmiş bir alarm ve gerekçesi."""

    alert_id: int
    email: str
    product_id: int
    product_title: str
    site_slug: str
    url: str
    old_price: Decimal | None
    new_price: Decimal | None
    currency: str | None
    percent: float | None
    reason: str


def _matches(alert: PriceAlert, change: PriceChange) -> str | None:
    """Alarm bu değişimle tetiklenir mi? Tetiklenirse gerekçeyi döndürür."""
    if change.new_price is None:
        return None

    if alert.target_price is not None and change.new_price <= Decimal(alert.target_price):
        return f"hedef fiyatın altına indi ({alert.target_price})"

    if alert.drop_percent is not None:
        percent = change.percent
        if percent is not None and percent <= -float(alert.drop_percent):
            return f"%{abs(percent):.1f} ucuzladı (eşik %{alert.drop_percent})"

    return None


def _in_cooldown(alert: PriceAlert, now: datetime, hours: int) -> bool:
    """Yakın zamanda tetiklendiyse tekrar gönderme.

    Fiyat eşiğin hemen altında salınırsa her kazımada e-posta gitmesin;
    kullanıcı bildirimleri kapatmaya başlar.
    """
    if alert.last_triggered_at is None:
        return False

    last = alert.last_triggered_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return now - last < timedelta(hours=hours)


async def evaluate_alerts(
    session: AsyncSession,
    changes: list[PriceChange],
    *,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> list[TriggeredAlert]:
    """Fiyat değişimlerini alarm kurallarıyla karşılaştırır.

    Tetiklenen alarmların `last_triggered_at` alanını günceller — çağıran
    taraf e-postayı göndermekle yükümlü.
    """
    drops = [change for change in changes if change.is_drop]
    if not drops:
        return []

    triggered: list[TriggeredAlert] = []
    now = datetime.now(timezone.utc)

    # PriceChange ürün başlığını taşıyor ama kimliğini taşımıyor; alarmlar
    # ürüne bağlı olduğu için teklif → ürün eşlemesini burada kuruyoruz.
    offer_rows = (
        await session.execute(
            select(Offer.id, Offer.product_id).where(
                Offer.id.in_([change.offer_id for change in drops])
            )
        )
    ).all()
    offer_to_product = {row.id: row.product_id for row in offer_rows}

    relevant_products = set(offer_to_product.values())
    if not relevant_products:
        return []

    alerts = (
        await session.execute(
            select(PriceAlert).where(
                PriceAlert.product_id.in_(relevant_products),
                PriceAlert.active.is_(True),
            )
        )
    ).scalars().all()

    if not alerts:
        return []

    titles = dict(
        (
            await session.execute(
                select(Product.id, Product.title).where(Product.id.in_(relevant_products))
            )
        ).all()
    )

    by_product: dict[int, list[PriceChange]] = {}
    for change in drops:
        product_id = offer_to_product.get(change.offer_id)
        if product_id:
            by_product.setdefault(product_id, []).append(change)

    for alert in alerts:
        if _in_cooldown(alert, now, cooldown_hours):
            logger.debug("Alarm %s soğuma süresinde, atlandı", alert.id)
            continue

        for change in by_product.get(alert.product_id, []):
            reason = _matches(alert, change)
            if reason is None:
                continue

            triggered.append(
                TriggeredAlert(
                    alert_id=alert.id,
                    email=alert.email,
                    product_id=alert.product_id,
                    product_title=titles.get(alert.product_id, change.product_title),
                    site_slug=change.site_slug,
                    url=change.url,
                    old_price=change.old_price,
                    new_price=change.new_price,
                    currency=change.currency,
                    percent=change.percent,
                    reason=reason,
                )
            )
            alert.last_triggered_at = now
            break  # aynı alarm için tek bildirim yeter

    if triggered:
        await session.flush()
        logger.info("%d alarm tetiklendi", len(triggered))

    return triggered
