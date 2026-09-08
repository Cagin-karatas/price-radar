"""Kazima tetikleme ve zamanlayici durumu."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import select

from ...db.models import ScrapeRun, Site
from ..deps import SessionDep
from ..schemas import (
    JobStatus,
    RunOut,
    ScrapeAccepted,
    ScrapeRequest,
    SchedulerStatus,
)

router = APIRouter(tags=["isler"])


def _manager(request: Request):
    manager = getattr(request.app.state, "scrape_manager", None)
    if manager is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Zamanlayici baslatilmamis"
        )
    return manager


@router.post(
    "/scrape",
    response_model=ScrapeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Kazimayi simdi tetikle",
)
async def trigger_scrape(
    payload: ScrapeRequest,
    background: BackgroundTasks,
    request: Request,
    session: SessionDep,
) -> ScrapeAccepted:
    """Kazimayi arka planda baslatir ve hemen doner.

    Kazima dakikalar surebilir; HTTP istegini o kadar bekletmek yerine
    202 donup isi arka plana atiyoruz. Ilerlemeyi `GET /runs` ile izle.
    """
    manager = _manager(request)

    if manager.is_scraping:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Kazima zaten suruyor, bitmesini bekle"
        )

    if payload.slugs:
        found = (
            await session.execute(select(Site.slug).where(Site.slug.in_(payload.slugs)))
        ).scalars().all()
        missing = set(payload.slugs) - set(found)
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Site bulunamadi: {', '.join(sorted(missing))}"
            )

    background.add_task(manager.run_scrape, payload.slugs)

    return ScrapeAccepted(
        status="accepted",
        detail="Kazima arka planda basladi. Ilerleme icin GET /runs.",
        slugs=payload.slugs,
    )


@router.get("/scheduler", response_model=SchedulerStatus, summary="Zamanlayici durumu")
async def scheduler_status(request: Request) -> SchedulerStatus:
    manager = _manager(request)
    return SchedulerStatus(
        running=manager.scheduler.running,
        scrape_interval_minutes=manager.settings.scrape_interval_minutes,
        jobs=[JobStatus(**job) for job in manager.jobs()],
    )


@router.get("/runs", response_model=list[RunOut], summary="Gecmis calistirmalar")
async def list_runs(
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
) -> list[ScrapeRun]:
    rows = (
        await session.execute(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit))
    ).scalars().all()
    return list(rows)


@router.get("/runs/{run_id}", response_model=RunOut, summary="Calistirma detayi")
async def get_run(run_id: int, session: SessionDep) -> ScrapeRun:
    run = await session.get(ScrapeRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Calistirma bulunamadi: {run_id}")
    return run
