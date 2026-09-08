"""CSV ve Excel disa aktarim uc noktalari."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response

from ...reports.export import DATASETS, build_export, build_workbook
from ..deps import SessionDep

router = APIRouter(prefix="/export", tags=["disa aktarim"])


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    # RFC 5987: dosya adinda Turkce karakter varsa bozulmasin
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/workbook.xlsx", summary="Tum veriyi tek Excel dosyasinda indir")
async def export_workbook(session: SessionDep) -> Response:
    """Urunler, teklifler ve fiyat gecmisi - uc sayfali tek dosya."""
    content, filename, media_type = await build_workbook(session)
    return _attachment(content, filename, media_type)


@router.get("/{dataset}.{fmt}", summary="Tek veri kumesi indir")
async def export_dataset(
    dataset: str,
    fmt: str,
    session: SessionDep,
    site: str | None = Query(None, description="Sadece bu site (offers icin)"),
    product_id: int | None = Query(None, description="Sadece bu urun (history icin)"),
) -> Response:
    """`dataset`: products / offers / history — `fmt`: csv / xlsx"""
    try:
        content, filename, media_type = await build_export(
            session, dataset, fmt, site=site, product_id=product_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return _attachment(content, filename, media_type)
