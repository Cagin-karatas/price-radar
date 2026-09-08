"""Toplama boru hattı: kazı → eşleştir → kaydet → değişimi tespit et.

Akış:

    Siteler → Adaptörler → ScrapedItem[] → ürün eşleştirme
        → Product / Offer upsert → fiyat değişti mi? → PriceSnapshot

Kritik karar: PriceSnapshot **her kontrolde değil, sadece değişimde** yazılır.
Saatte bir çalışan bir sistemde 1000 ürün için günde 24.000 satır yerine
sadece gerçekten değişenler kaydedilir. Grafik çizerken "son bilinen değer"
mantığıyla okunur.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Offer, PriceSnapshot, Product, ScrapeRun, Site
from .matching import find_match, fingerprint
from .normalize import Availability
from .scraping.base import ScrapedItem, SiteConfig, build_adapter

logger = logging.getLogger(__name__)


@dataclass
class PriceChange:
    """Tespit edilen bir fiyat değişimi."""

    offer_id: int
    product_title: str
    site_slug: str
    url: str
    old_price: Decimal | None
    new_price: Decimal | None
    currency: str | None

    @property
    def is_drop(self) -> bool:
        return (
            self.old_price is not None
            and self.new_price is not None
            and self.new_price < self.old_price
        )

    @property
    def percent(self) -> float | None:
        if not self.old_price or self.new_price is None or self.old_price == 0:
            return None
        return float((self.new_price - self.old_price) / self.old_price * 100)


@dataclass
class IngestResult:
    """Bir çalıştırmanın sonucu."""

    items_found: int = 0
    products_created: int = 0
    offers_created: int = 0
    duplicates_merged: int = 0
    price_changes: list[PriceChange] = field(default_factory=list)
    availability_changes: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    per_site: dict[str, int] = field(default_factory=dict)

    @property
    def drops(self) -> list[PriceChange]:
        return [change for change in self.price_changes if change.is_drop]


async def scrape_sites(
    configs: list[SiteConfig], client
) -> tuple[list[ScrapedItem], dict[str, str], dict[str, int]]:
    """Tüm siteleri eşzamanlı kazır.

    `asyncio.gather` ile paralel çalışıyorlar; hız sınırı alan adı başına
    uygulandığından bu tek bir siteyi yormaz, sadece farklı siteleri
    aynı anda gezer.
    """
    enabled = [config for config in configs if config.enabled]
    adapters = [build_adapter(config, client) for config in enabled]

    results = await asyncio.gather(*(adapter.safe_scrape() for adapter in adapters))

    items: list[ScrapedItem] = []
    errors: dict[str, str] = {}
    per_site: dict[str, int] = {}

    for config, (site_items, error) in zip(enabled, results):
        per_site[config.slug] = len(site_items)
        if error:
            errors[config.slug] = str(error)
        items.extend(site_items)

    return items, errors, per_site


async def _get_or_create_site(session: AsyncSession, config: SiteConfig) -> Site:
    site = await session.scalar(select(Site).where(Site.slug == config.slug))
    if site:
        return site

    site = Site(
        slug=config.slug,
        name=config.name,
        base_url=config.base_url,
        adapter=config.adapter,
        enabled=config.enabled,
    )
    session.add(site)
    await session.flush()
    return site


async def _resolve_product(
    session: AsyncSession,
    item: ScrapedItem,
    result: IngestResult,
    *,
    threshold: float = 0.82,
) -> Product:
    """İlanı bir ürüne bağlar: varsa mevcut, yoksa yeni.

    Önce parmak izi ile kesin eşleşme aranır (hızlı, indeksli). Bulunamazsa
    bulanık eşleştirmeye düşülür — bu, aynı ürünün farklı sitelerde farklı
    başlıklarla listelenmesini yakalar.
    """
    print_ = fingerprint(item.title, item.brand)

    product = await session.scalar(select(Product).where(Product.fingerprint == print_))
    if product:
        return product

    # Bulanık eşleştirme: aynı markadaki ürünler arasında ara.
    # Marka yoksa tüm ürünlere bakmak N² olur; o yüzden başlığın ilk
    # anlamlı belirtecine göre daraltıyoruz.
    candidates_query = select(Product.id, Product.title)
    if item.brand:
        candidates_query = candidates_query.where(Product.brand == item.brand)

    rows = (await session.execute(candidates_query.limit(2000))).all()
    candidates = [(row[0], row[1]) for row in rows]

    found = find_match(item.title, candidates, brand=item.brand, threshold=threshold)
    if found:
        product_id, match_result = found
        product = await session.get(Product, product_id)
        if product:
            result.duplicates_merged += 1
            logger.debug(
                "Duplicate birleştirildi (%s): %r ↔ %r",
                match_result.reason,
                item.title[:40],
                product.title[:40],
            )
            return product

    product = Product(
        fingerprint=print_,
        title=item.title,
        brand=item.brand,
        category=item.category,
    )
    session.add(product)
    await session.flush()
    result.products_created += 1
    return product


async def ingest_items(
    session: AsyncSession,
    items: list[ScrapedItem],
    site_configs: dict[str, SiteConfig],
) -> IngestResult:
    """Kazınmış ürünleri veritabanına işler."""
    result = IngestResult(items_found=len(items))
    now = datetime.now(timezone.utc)
    site_cache: dict[str, Site] = {}

    for item in items:
        config = site_configs.get(item.site_slug)
        if config is None:
            logger.warning("Tanımsız site atlandı: %s", item.site_slug)
            continue

        if item.site_slug not in site_cache:
            site_cache[item.site_slug] = await _get_or_create_site(session, config)
        site = site_cache[item.site_slug]

        product = await _resolve_product(session, item, result)

        offer = await session.scalar(
            select(Offer).where(Offer.site_id == site.id, Offer.url == item.url)
        )

        if offer is None:
            offer = Offer(
                product_id=product.id,
                site_id=site.id,
                url=item.url,
                external_id=item.external_id,
                raw_title=item.title,
                last_price=item.price,
                last_currency=item.currency,
                last_availability=item.availability.value,
                last_checked_at=now,
            )
            session.add(offer)
            await session.flush()
            result.offers_created += 1

            # İlk görüşte de anlık görüntü yaz: geçmişin başlangıç noktası
            session.add(
                PriceSnapshot(
                    offer_id=offer.id,
                    price=item.price,
                    currency=item.currency,
                    availability=item.availability.value,
                    captured_at=now,
                )
            )
            continue

        price_changed = _price_differs(offer.last_price, item.price)
        availability_changed = offer.last_availability != item.availability.value

        if price_changed:
            result.price_changes.append(
                PriceChange(
                    offer_id=offer.id,
                    product_title=product.title,
                    site_slug=site.slug,
                    url=offer.url,
                    old_price=offer.last_price,
                    new_price=item.price,
                    currency=item.currency or offer.last_currency,
                )
            )

        if availability_changed:
            result.availability_changes += 1

        if price_changed or availability_changed:
            session.add(
                PriceSnapshot(
                    offer_id=offer.id,
                    price=item.price,
                    currency=item.currency,
                    availability=item.availability.value,
                    captured_at=now,
                )
            )

        offer.last_price = item.price
        offer.last_currency = item.currency
        offer.last_availability = item.availability.value
        offer.last_checked_at = now
        offer.raw_title = item.title
        offer.active = True

    return result


def _price_differs(old: Decimal | None, new: Decimal | None) -> bool:
    """Fiyat değişti mi?

    Decimal karşılaştırması gerekiyor: veritabanından Decimal('51.77'),
    kazıyıcıdan Decimal('51.770') gelebilir ve bunlar eşittir.
    """
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return Decimal(old) != Decimal(new)


async def run_collection(
    session: AsyncSession,
    configs: list[SiteConfig],
    client,
) -> tuple[IngestResult, ScrapeRun]:
    """Uçtan uca: kazı, işle, çalıştırmayı kaydet."""
    run = ScrapeRun(sites=",".join(config.slug for config in configs))
    session.add(run)
    await session.flush()

    items, errors, per_site = await scrape_sites(configs, client)

    result = await ingest_items(session, items, {c.slug: c for c in configs})
    result.errors = errors
    result.per_site = per_site

    run.finished_at = datetime.now(timezone.utc)
    run.items_found = result.items_found
    run.products_created = result.products_created
    run.offers_created = result.offers_created
    run.price_changes = len(result.price_changes)
    run.duplicates_merged = result.duplicates_merged
    run.errors = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None

    logger.info(
        "%d ürün | %d yeni ürün | %d yeni teklif | %d fiyat değişimi | %d duplicate",
        result.items_found,
        result.products_created,
        result.offers_created,
        len(result.price_changes),
        result.duplicates_merged,
    )

    return result, run
