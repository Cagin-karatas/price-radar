"""FastAPI uygulaması.

Açılışta: veritabanı hazırlanır, YAML site tanımları senkronize edilir,
zamanlayıcı başlar. Kapanışta zamanlayıcı ve bağlantı havuzu kapatılır.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from .. import __version__
from ..config import Settings, load_site_configs
from ..db.models import Product, Site
from ..db.session import dispose_db, init_db, session_scope
from ..scheduler import ScrapeManager
from ..scraping.client import FetchError
from ..sites_repo import sync_yaml_sites
from .deps import SessionDep, get_settings
from .routers import alerts, exports, jobs, offers, products, sites
from .schemas import HealthOut

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"

DESCRIPTION = """
Çok siteli fiyat ve stok takip platformu.

* **Siteler** — takip edilecek siteleri kod yazmadan ekle (CSS seçicileriyle)
* **Ürünler** — aynı ürünün farklı sitelerdeki fiyatlarını karşılaştır
* **Geçmiş** — fiyat değişimlerini zaman serisi olarak al
* **İşler** — kazımayı elle tetikle veya zamanlanmış çalıştırmayı izle
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    await init_db(settings.database_url, echo=settings.echo_sql)

    # YAML tanımlarını veritabanına tohumla
    configs = load_site_configs(settings.sites_dir)
    if configs:
        async with session_scope() as session:
            stats = await sync_yaml_sites(session, configs)
        logger.info(
            "YAML siteleri senkronize edildi: %d yeni, %d güncel, %d atlandı",
            stats["created"],
            stats["updated"],
            stats["skipped"],
        )

    manager = ScrapeManager(settings)
    manager.start()
    app.state.scrape_manager = manager

    try:
        yield
    finally:
        manager.shutdown()
        await dispose_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.api_title,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(FetchError)
    async def fetch_error_handler(request: Request, exc: FetchError):
        """Kazıma hatası sunucu hatası değil: hedef site cevap vermiyor."""
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc), "url": exc.url, "status": exc.status},
        )

    @app.get("/health", response_model=HealthOut, tags=["sistem"])
    async def health(session: SessionDep, request: Request) -> HealthOut:
        """Sağlık kontrolü. Docker healthcheck ve izleme için."""
        manager = getattr(request.app.state, "scrape_manager", None)

        try:
            site_count = await session.scalar(
                select(func.count(Site.id)).where(Site.enabled.is_(True))
            )
            product_count = await session.scalar(select(func.count(Product.id)))
            database = "ok"
        except Exception as exc:  # noqa: BLE001
            logger.error("Sağlık kontrolü veritabanına ulaşamadı: %s", exc)
            site_count = product_count = 0
            database = "error"

        return HealthOut(
            status="ok" if database == "ok" else "degraded",
            version=__version__,
            database=database,
            scheduler_running=bool(manager and manager.scheduler.running),
            sites_enabled=site_count or 0,
            products=product_count or 0,
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        """Tek dosyalık pano. Derleme adımı ve CDN bağımlılığı yok."""
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    app.include_router(sites.router)
    app.include_router(products.router)
    app.include_router(offers.router)
    app.include_router(alerts.router)
    app.include_router(exports.router)
    app.include_router(jobs.router)

    return app


app = create_app()
