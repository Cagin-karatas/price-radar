"""Urun ve teklif uc noktalari."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ...db.models import Offer, PriceSnapshot, Product, Site
from ..deps import SessionDep
from ..schemas import OfferOut, ProductDetail, ProductSummary, SnapshotOut

router = APIRouter(prefix="/products", tags=["urunler"])


def _offer_out(offer: Offer, site_slug: str) -> OfferOut:
    return OfferOut(
        id=offer.id,
        product_id=offer.product_id,
        site_slug=site_slug,
        url=offer.url,
        raw_title=offer.raw_title,
        last_price=offer.last_price,
        last_currency=offer.last_currency,
        last_availability=offer.last_availability,
        last_checked_at=offer.last_checked_at,
    )


@router.get("", response_model=list[ProductSummary], summary="Urunleri listele")
async def list_products(
    session: SessionDep,
    search: str | None = Query(None, description="Baslikta ara"),
    min_offers: int = Query(1, ge=1, description="En az bu kadar teklifi olanlar"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ProductSummary]:
    """Urunleri en dusuk/en yuksek fiyatlariyla listeler.

    `min_offers=2` vererek yalnizca birden fazla sitede bulunan urunleri
    gorebilirsin - fiyat karsilastirmasi asil orada anlamli.
    """
    query = (
        select(
            Product.id,
            Product.title,
            Product.brand,
            func.count(Offer.id).label("offer_count"),
            func.min(Offer.last_price).label("min_price"),
            func.max(Offer.last_price).label("max_price"),
            func.min(Offer.last_currency).label("currency"),
        )
        .join(Offer, Offer.product_id == Product.id)
        .group_by(Product.id, Product.title, Product.brand)
        .having(func.count(Offer.id) >= min_offers)
        .order_by(Product.id.desc())
        .limit(limit)
        .offset(offset)
    )

    if search:
        query = query.where(Product.title.ilike(f"%{search}%"))

    rows = (await session.execute(query)).all()
    return [
        ProductSummary(
            id=row.id,
            title=row.title,
            brand=row.brand,
            offer_count=row.offer_count,
            min_price=row.min_price,
            max_price=row.max_price,
            currency=row.currency,
        )
        for row in rows
    ]


@router.get("/{product_id}", response_model=ProductDetail, summary="Urun detayi")
async def get_product(product_id: int, session: SessionDep) -> ProductDetail:
    product = await session.scalar(
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.offers).selectinload(Offer.site))
    )
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Urun bulunamadi: {product_id}")

    return ProductDetail(
        id=product.id,
        title=product.title,
        brand=product.brand,
        category=product.category,
        created_at=product.created_at,
        offers=[_offer_out(offer, offer.site.slug) for offer in product.offers],
    )


@router.get(
    "/{product_id}/history",
    response_model=dict[str, list[SnapshotOut]],
    summary="Fiyat gecmisi",
)
async def product_history(
    product_id: int,
    session: SessionDep,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, list[SnapshotOut]]:
    """Urunun tum tekliflerinin fiyat gecmisi, site slug'ina gore gruplanmis.

    Grafik cizmek icin dogrudan kullanilabilir: her site bir seri.
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Urun bulunamadi: {product_id}")

    rows = (
        await session.execute(
            select(PriceSnapshot, Site.slug)
            .join(Offer, Offer.id == PriceSnapshot.offer_id)
            .join(Site, Site.id == Offer.site_id)
            .where(Offer.product_id == product_id)
            .order_by(PriceSnapshot.captured_at)
            .limit(limit)
        )
    ).all()

    history: dict[str, list[SnapshotOut]] = {}
    for snapshot, slug in rows:
        history.setdefault(slug, []).append(SnapshotOut.model_validate(snapshot))
    return history
