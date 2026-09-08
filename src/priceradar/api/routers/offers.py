"""Teklif uc noktalari."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...db.models import Offer, PriceSnapshot, Site
from ..deps import SessionDep
from ..schemas import OfferOut, SnapshotOut

router = APIRouter(prefix="/offers", tags=["teklifler"])


@router.get("", response_model=list[OfferOut], summary="Teklifleri listele")
async def list_offers(
    session: SessionDep,
    site: str | None = Query(None, description="Site slug'ina gore filtrele"),
    availability: str | None = Query(None, description="in_stock / out_of_stock"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[OfferOut]:
    query = (
        select(Offer)
        .options(selectinload(Offer.site))
        .order_by(Offer.id.desc())
        .limit(limit)
        .offset(offset)
    )

    if site:
        query = query.join(Site).where(Site.slug == site)
    if availability:
        query = query.where(Offer.last_availability == availability)

    offers = (await session.execute(query)).scalars().all()
    return [
        OfferOut(
            id=offer.id,
            product_id=offer.product_id,
            site_slug=offer.site.slug,
            url=offer.url,
            raw_title=offer.raw_title,
            last_price=offer.last_price,
            last_currency=offer.last_currency,
            last_availability=offer.last_availability,
            last_checked_at=offer.last_checked_at,
        )
        for offer in offers
    ]


@router.get(
    "/{offer_id}/history",
    response_model=list[SnapshotOut],
    summary="Teklifin fiyat gecmisi",
)
async def offer_history(
    offer_id: int,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
) -> list[SnapshotOut]:
    offer = await session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Teklif bulunamadi: {offer_id}")

    snapshots = (
        await session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.offer_id == offer_id)
            .order_by(PriceSnapshot.captured_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [SnapshotOut.model_validate(s) for s in snapshots]
