"""Fiyat alarmi CRUD."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from ...alerts import TriggeredAlert
from ...db.models import PriceAlert, Product
from ...notifications.email import render_alert_email
from ..deps import SessionDep
from ..schemas import AlertCreate, AlertOut, AlertTestOut, AlertUpdate

router = APIRouter(prefix="/alerts", tags=["alarmlar"])


def _to_out(alert: PriceAlert, title: str | None = None) -> AlertOut:
    return AlertOut(
        id=alert.id,
        product_id=alert.product_id,
        product_title=title,
        email=alert.email,
        target_price=alert.target_price,
        drop_percent=alert.drop_percent,
        active=alert.active,
        last_triggered_at=alert.last_triggered_at,
        created_at=alert.created_at,
    )


@router.get("", response_model=list[AlertOut], summary="Alarmlari listele")
async def list_alerts(
    session: SessionDep,
    email: str | None = Query(None, description="Aliciya gore filtrele"),
    active: bool | None = None,
) -> list[AlertOut]:
    query = select(PriceAlert, Product.title).join(
        Product, Product.id == PriceAlert.product_id
    )
    if email:
        query = query.where(PriceAlert.email == email)
    if active is not None:
        query = query.where(PriceAlert.active.is_(active))

    rows = (await session.execute(query.order_by(PriceAlert.id.desc()))).all()
    return [_to_out(alert, title) for alert, title in rows]


@router.post(
    "", response_model=AlertOut, status_code=status.HTTP_201_CREATED, summary="Alarm ekle"
)
async def create_alert(payload: AlertCreate, session: SessionDep) -> AlertOut:
    """Bir urun icin fiyat alarmi tanimlar.

    `target_price` mutlak esik, `drop_percent` goreli dusus. Ikisi birden
    verilirse biri saglandiginda tetiklenir.
    """
    product = await session.get(Product, payload.product_id)
    if product is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Urun bulunamadi: {payload.product_id}"
        )

    existing = await session.scalar(
        select(PriceAlert).where(
            PriceAlert.product_id == payload.product_id,
            PriceAlert.email == payload.email,
        )
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Bu urun icin bu adrese tanimli alarm zaten var",
        )

    alert = PriceAlert(**payload.model_dump())
    session.add(alert)
    await session.flush()
    return _to_out(alert, product.title)


@router.patch("/{alert_id}", response_model=AlertOut, summary="Alarmi guncelle")
async def update_alert(alert_id: int, payload: AlertUpdate, session: SessionDep) -> AlertOut:
    alert = await session.get(PriceAlert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Alarm bulunamadi: {alert_id}")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(alert, field, value)

    if alert.target_price is None and alert.drop_percent is None:
        raise HTTPException(
            422, "target_price veya drop_percent'ten en az biri tanimli kalmali"
        )

    await session.flush()
    product = await session.get(Product, alert.product_id)
    return _to_out(alert, product.title if product else None)


@router.delete(
    "/{alert_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Alarmi sil"
)
async def delete_alert(alert_id: int, session: SessionDep) -> Response:
    alert = await session.get(PriceAlert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Alarm bulunamadi: {alert_id}")

    await session.delete(alert)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{alert_id}/preview", response_model=AlertTestOut, summary="Bildirim onizlemesi"
)
async def preview_alert(alert_id: int, session: SessionDep) -> AlertTestOut:
    """Alarm tetiklenseydi gonderilecek e-postayi gosterir.

    SMTP ayarlarini denemeden once sablonun nasil gorundugunu kontrol etmek icin.
    """
    alert = await session.get(PriceAlert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Alarm bulunamadi: {alert_id}")

    product = await session.get(Product, alert.product_id)
    sample = TriggeredAlert(
        alert_id=alert.id,
        email=alert.email,
        product_id=alert.product_id,
        product_title=product.title if product else "Ornek urun",
        site_slug="ornek-site",
        url="https://ornek.com/urun",
        old_price=Decimal("1000.00"),
        new_price=Decimal("799.00"),
        currency="TRY",
        percent=-20.1,
        reason="ornek tetikleme",
    )

    subject, _, html = render_alert_email([sample])
    return AlertTestOut(subject=subject, html=html)
