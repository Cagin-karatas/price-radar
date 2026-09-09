"""CSV ve Excel disa aktarim uc noktalari."""

from __future__ import annotations

import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response

from ...reports.export import DATASETS, build_export, build_workbook
from ..deps import SessionDep

router = APIRouter(prefix="/export", tags=["disa aktarim"])


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    """Indirme basligi.

    Iki bicim birden gonderiliyor: duz `filename=` ve RFC 5987 `filename*=`.
    curl'un -J secenegi yalnizca duz bicimi okuyor, uzun bicimi gormezden
    gelip URL'nin son parcasina dusuyor. Tarayicilar ikisini de anliyor ve
    varsa `filename*`i tercih ediyor.
    """
    ascii_name = unicodedata.normalize("NFKD", filename)
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii") or "export"

    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
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
