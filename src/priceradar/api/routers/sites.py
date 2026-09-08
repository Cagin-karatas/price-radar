"""Site CRUD - kullanicinin takip edecegi siteleri yonetmesi."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from ...db.models import Offer, Site
from ...scraping.base import available_adapters
from ..deps import SessionDep
from ..schemas import SiteCreate, SiteOut, SiteUpdate

router = APIRouter(prefix="/sites", tags=["siteler"])


@router.get("", response_model=list[SiteOut], summary="Siteleri listele")
async def list_sites(session: SessionDep, enabled: bool | None = None) -> list[Site]:
    query = select(Site).order_by(Site.slug)
    if enabled is not None:
        query = query.where(Site.enabled.is_(enabled))
    return list((await session.execute(query)).scalars().all())


@router.post(
    "",
    response_model=SiteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Yeni site ekle",
)
async def create_site(payload: SiteCreate, session: SessionDep) -> Site:
    """Kod yazmadan yeni bir kaynak site tanimlar.

    Seciciler dogru mu emin degilsen once `POST /sites/preview` ile dene.
    """
    existing = await session.scalar(select(Site).where(Site.slug == payload.slug))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, f"'{payload.slug}' zaten var")

    if payload.adapter not in available_adapters():
        raise HTTPException(
            422,  # Starlette sabiti sürüme göre ad değiştirdi, sayı sabit
            f"Bilinmeyen adaptor: {payload.adapter}. "
            f"Secenekler: {', '.join(sorted(available_adapters()))}",
        )

    site = Site(**payload.model_dump(), managed_by="api")
    session.add(site)
    await session.flush()
    return site


@router.get("/{slug}", response_model=SiteOut, summary="Site detayi")
async def get_site(slug: str, session: SessionDep) -> Site:
    site = await session.scalar(select(Site).where(Site.slug == slug))
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site bulunamadi: {slug}")
    return site


@router.patch("/{slug}", response_model=SiteOut, summary="Siteyi guncelle")
async def update_site(slug: str, payload: SiteUpdate, session: SessionDep) -> Site:
    site = await session.scalar(select(Site).where(Site.slug == slug))
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site bulunamadi: {slug}")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)

    # Arayuzden degistirildiyse artik YAML senkronizasyonu ustune yazmasin
    site.managed_by = "api"
    await session.flush()
    return site


@router.delete(
    "/{slug}", status_code=status.HTTP_204_NO_CONTENT, summary="Siteyi sil"
)
async def delete_site(slug: str, session: SessionDep, force: bool = False) -> Response:
    """Siteyi siler.

    Site silinince tekliflerinin fiyat gecmisi de gider. Veri kaybini
    kazayla yapmamak icin teklifi olan siteler `force=true` ister.
    """
    site = await session.scalar(select(Site).where(Site.slug == slug))
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site bulunamadi: {slug}")

    offer_count = await session.scalar(
        select(func.count(Offer.id)).where(Offer.site_id == site.id)
    )
    if offer_count and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Bu sitenin {offer_count} teklifi ve fiyat gecmisi var. "
            "Silmek icin ?force=true ekle veya siteyi enabled=false yap.",
        )

    await session.delete(site)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
