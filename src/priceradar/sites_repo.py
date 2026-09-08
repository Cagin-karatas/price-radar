"""Site tanimlarinin veritabani ile YAML arasinda senkronizasyonu.

Tasarim karari: **tek dogruluk kaynagi veritabani**. YAML dosyalari bir
tohumlama mekanizmasi - acilista veritabanina yazilirlar. Boylece hem
"repoya YAML koyup versiyonla" hem "API'den site ekle" akislari birlikte
calisir.

`managed_by` alani cakismayi onluyor: YAML senkronizasyonu yalnizca
kendi yazdigi kayitlari gunceller, API'den eklenenlere dokunmaz.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Site
from .scraping.base import SiteConfig

logger = logging.getLogger(__name__)


def site_to_config(site: Site) -> SiteConfig:
    """Veritabani kaydini kaziyicinin anladigi yapilandirmaya cevirir."""
    return SiteConfig(
        slug=site.slug,
        name=site.name,
        base_url=site.base_url,
        adapter=site.adapter,
        start_urls=list(site.start_urls or []),
        currency=site.currency,
        enabled=site.enabled,
        max_pages=site.max_pages,
        selectors=dict(site.selectors or {}),
        pagination=dict(site.pagination or {}),
        browser=dict(site.browser or {}),
    )


def apply_config(site: Site, config: SiteConfig) -> bool:
    """Yapilandirmayi kayda uygular; bir sey degistiyse True doner."""
    fields = {
        "name": config.name,
        "base_url": config.base_url,
        "adapter": config.adapter,
        "currency": config.currency,
        "max_pages": config.max_pages,
        "start_urls": list(config.start_urls),
        "selectors": dict(config.selectors),
        "pagination": dict(config.pagination),
        "browser": dict(config.browser),
        "enabled": config.enabled,
    }

    changed = False
    for key, value in fields.items():
        if getattr(site, key) != value:
            setattr(site, key, value)
            changed = True
    return changed


async def sync_yaml_sites(session: AsyncSession, configs: list[SiteConfig]) -> dict[str, int]:
    """YAML tanimlarini veritabanina yazar.

    API'den eklenen siteler (managed_by='api') atlanir: kullanicinin
    arayuzden yaptigi degisiklik dosya senkronizasyonuyla silinmemeli.
    """
    created = updated = skipped = 0

    for config in configs:
        site = await session.scalar(select(Site).where(Site.slug == config.slug))

        if site is None:
            site = Site(slug=config.slug, managed_by="yaml")
            apply_config(site, config)
            session.add(site)
            created += 1
            continue

        if site.managed_by == "api":
            skipped += 1
            logger.debug("%s API'den yonetiliyor, YAML atlandi", config.slug)
            continue

        if apply_config(site, config):
            updated += 1

    await session.flush()
    return {"created": created, "updated": updated, "skipped": skipped}


async def load_enabled_configs(
    session: AsyncSession, slugs: list[str] | None = None
) -> list[SiteConfig]:
    """Kazinacak site yapilandirmalarini veritabanindan okur."""
    query = select(Site)
    if slugs:
        query = query.where(Site.slug.in_(slugs))
    else:
        query = query.where(Site.enabled.is_(True))

    sites = (await session.execute(query.order_by(Site.slug))).scalars().all()
    return [site_to_config(site) for site in sites]
