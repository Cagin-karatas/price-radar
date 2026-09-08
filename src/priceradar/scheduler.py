"""Arka plan işleri.

APScheduler seçildi, Celery değil. Gerekçe: bu iş yükü tek bir periyodik
görev — "her N dakikada bir siteleri kazı". Celery ayrı bir broker (Redis),
ayrı worker süreci ve ayrı dağıtım karmaşıklığı getiriyor; karşılığında
aldığın dağıtık kuyruk ve iş yeniden dağıtımı bu ölçekte kullanılmıyor.

Ne zaman Celery'ye geçilmeli: birden çok worker makinesi gerektiğinde,
ya da işler süreç çökmesinde kaybolmamalıysa. O gün geldiğinde `run_scrape`
zaten bağımsız bir fonksiyon — Celery task'ına sarmak birkaç satır.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .db.session import session_scope
from .pipeline import run_collection
from .scraping.client import AsyncScraperClient
from .sites_repo import load_enabled_configs

logger = logging.getLogger(__name__)

SCRAPE_JOB_ID = "scrape-all-sites"


class ScrapeManager:
    """Kazıma işlerini yönetir ve aynı anda ikinci çalıştırmayı engeller.

    Kilit önemli: 30 dakikalık zamanlanmış iş henüz bitmeden kullanıcı
    elle tetiklerse iki kazıma çakışır, aynı siteye iki kat istek gider
    ve hız sınırı anlamsızlaşır.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._lock = asyncio.Lock()
        self._running = False
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def is_scraping(self) -> bool:
        return self._running

    def _build_client(self) -> AsyncScraperClient:
        return AsyncScraperClient(
            user_agent=self.settings.user_agent,
            min_interval=self.settings.min_interval,
            max_concurrency=self.settings.max_concurrency,
            max_retries=self.settings.max_retries,
            timeout=self.settings.request_timeout,
            respect_robots=self.settings.respect_robots,
            proxies=self.settings.proxies,
        )

    async def run_scrape(self, slugs: list[str] | None = None) -> dict:
        """Kazımayı çalıştırır. Zaten çalışıyorsa atlar."""
        if self._lock.locked():
            logger.warning("Kazıma zaten sürüyor, yeni istek atlandı")
            return {"status": "skipped", "detail": "Kazıma zaten sürüyor"}

        async with self._lock:
            self._running = True
            self.last_started_at = datetime.now()
            self.last_error = None

            try:
                async with session_scope() as session:
                    configs = await load_enabled_configs(session, slugs)

                if not configs:
                    return {"status": "empty", "detail": "Etkin site yok"}

                async with self._build_client() as client:
                    async with session_scope() as session:
                        result, run = await run_collection(session, configs, client)
                        summary = {
                            "status": "ok",
                            "run_id": run.id,
                            "items_found": result.items_found,
                            "products_created": result.products_created,
                            "offers_created": result.offers_created,
                            "price_changes": len(result.price_changes),
                            "drops": len(result.drops),
                            "duplicates_merged": result.duplicates_merged,
                            "errors": result.errors,
                        }

                logger.info("Kazıma bitti: %s", summary)
                return summary

            except Exception as exc:  # noqa: BLE001
                # Zamanlanmış iş içindeki hata sessizce kaybolmasın:
                # APScheduler istisnayı yutar, biz kaydediyoruz.
                self.last_error = str(exc)
                logger.exception("Kazıma başarısız")
                return {"status": "error", "detail": str(exc)}
            finally:
                self._running = False
                self.last_finished_at = datetime.now()

    def start(self) -> None:
        """Zamanlayıcıyı başlatır."""
        minutes = self.settings.scrape_interval_minutes
        if minutes <= 0:
            logger.info("Zamanlanmış kazıma kapalı (interval=%s)", minutes)
            return

        self.scheduler.add_job(
            self.run_scrape,
            trigger=IntervalTrigger(minutes=minutes),
            id=SCRAPE_JOB_ID,
            name="Tüm siteleri kazı",
            # Uygulama bir süre kapalı kaldıysa birikmiş çalıştırmalar
            # arka arkaya tetiklenmesin
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        self.scheduler.start()
        logger.info("Zamanlayıcı başladı: her %d dakikada bir", minutes)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Zamanlayıcı durdu")

    def jobs(self) -> list[dict]:
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_at": getattr(job, "next_run_time", None),
                "trigger": str(job.trigger),
            }
            for job in self.scheduler.get_jobs()
        ]
