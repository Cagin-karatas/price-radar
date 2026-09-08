"""Komut satırı arayüzü.

    priceradar init-db              # tabloları oluştur
    priceradar sites                # tanımlı siteleri listele
    priceradar scrape               # topla ve kaydet
    priceradar scrape --site books-toscrape --dry-run
    priceradar products             # takip edilen ürünler
    priceradar history <offer_id>   # bir teklifin fiyat geçmişi
    priceradar robots <url>         # robots.txt teşhisi
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlalchemy import func, select

from . import __version__
from .config import Settings, load_site_configs
from .db.models import Offer, PriceSnapshot, Product, ScrapeRun, Site
from .db.session import dispose_db, init_db, session_scope
from .pipeline import run_collection
from .scraping.base import available_adapters
from .scraping.client import AsyncScraperClient

app = typer.Typer(add_completion=False, help="Çok siteli fiyat ve stok takip platformu.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    # Kütüphanelerin kendi DEBUG logları kendi çıktımızı boğuyor:
    # httpx her isteği, aiosqlite/SQLAlchemy her bağlantı işlemini basıyor.
    for noisy in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_client(settings: Settings) -> AsyncScraperClient:
    return AsyncScraperClient(
        user_agent=settings.user_agent,
        min_interval=settings.min_interval,
        max_concurrency=settings.max_concurrency,
        max_retries=settings.max_retries,
        timeout=settings.request_timeout,
        respect_robots=settings.respect_robots,
        proxies=settings.proxies,
    )


@app.command(name="init-db")
def init_db_command(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Veritabanı tablolarını oluşturur."""
    _setup_logging(verbose)
    settings = Settings()

    async def _run():
        await init_db(settings.database_url, echo=settings.echo_sql)
        await dispose_db()

    asyncio.run(_run())
    console.print(f"[green]✓[/] Tablolar hazır → [bold]{settings.database_url}[/]")


@app.command()
def sites(
    sites_dir: Path = typer.Option(None, "--sites-dir", help="Site tanımları klasörü"),
) -> None:
    """Tanımlı siteleri listeler."""
    settings = Settings()
    configs = load_site_configs(sites_dir or settings.sites_dir)

    if not configs:
        console.print("Tanımlı site yok. `config/sites/` altına bir YAML ekle.")
        return

    table = Table(header_style="bold cyan")
    table.add_column("Slug")
    table.add_column("Ad")
    table.add_column("Adaptör")
    table.add_column("Durum")
    table.add_column("Başlangıç URL")

    for config in configs:
        table.add_row(
            config.slug,
            config.name,
            config.adapter,
            "[green]açık[/]" if config.enabled else "[dim]kapalı[/]",
            (config.start_urls[0] if config.start_urls else "—")[:58],
        )

    console.print(table)
    console.print(f"\nKullanılabilir adaptörler: {', '.join(sorted(available_adapters()))}")


@app.command()
def scrape(
    site: list[str] = typer.Option(None, "--site", "-s", help="Sadece bu slug'ları çalıştır"),
    sites_dir: Path = typer.Option(None, "--sites-dir"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Veritabanına yazma, sadece göster"),
    limit: int = typer.Option(None, "--limit", help="Site başına sayfa sınırı"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Siteleri kazır, ürünleri eşleştirir, fiyat değişimlerini kaydeder."""
    _setup_logging(verbose)
    settings = Settings()

    configs = load_site_configs(sites_dir or settings.sites_dir)
    if site:
        wanted = set(site)
        configs = [c for c in configs if c.slug in wanted]
        for config in configs:
            config.enabled = True  # açıkça istendiyse kapalı olsa da çalıştır

    configs = [c for c in configs if c.enabled]
    if not configs:
        console.print("[bold red]Çalıştırılacak site yok.[/] `priceradar sites` ile kontrol et.")
        raise typer.Exit(code=1)

    if limit:
        for config in configs:
            config.max_pages = limit

    asyncio.run(_scrape_async(settings, configs, dry_run))


async def _scrape_async(settings: Settings, configs, dry_run: bool) -> None:
    console.print(f"[bold]{len(configs)} site kazınıyor…[/]")

    if dry_run:
        from .pipeline import scrape_sites

        async with _build_client(settings) as client:
            items, errors, per_site = await scrape_sites(configs, client)

        table = Table(title=f"{len(items)} ürün bulundu (kaydedilmedi)", header_style="bold cyan")
        table.add_column("Site")
        table.add_column("Ürün")
        table.add_column("Fiyat", justify="right")
        table.add_column("Stok")

        for item in items[:40]:
            table.add_row(
                item.site_slug,
                item.title[:48],
                f"{item.price} {item.currency or ''}" if item.price else "—",
                item.availability.value,
            )

        console.print(table)
        if len(items) > 40:
            console.print(f"[dim]… ve {len(items) - 40} ürün daha[/]")
        for slug, message in errors.items():
            console.print(f"[yellow]Hata ({slug}):[/] {message}")
        return

    await init_db(settings.database_url, echo=settings.echo_sql)

    async with _build_client(settings) as client:
        async with session_scope() as session:
            result, run = await run_collection(session, configs, client)

    _print_result(result, client)
    await dispose_db()


def _print_result(result, client) -> None:
    table = Table(title="Toplama özeti", header_style="bold cyan")
    table.add_column("Metrik")
    table.add_column("Değer", justify="right")

    table.add_row("Bulunan ürün", str(result.items_found))
    table.add_row("Yeni ürün", str(result.products_created))
    table.add_row("Yeni teklif", str(result.offers_created))
    table.add_row("Birleştirilen duplicate", str(result.duplicates_merged))
    table.add_row("Fiyat değişimi", str(len(result.price_changes)))
    table.add_row("Fiyat düşüşü", f"[green]{len(result.drops)}[/]")
    table.add_row("Stok değişimi", str(result.availability_changes))
    for slug, count in result.per_site.items():
        table.add_row(f"Site: {slug}", str(count))
    for slug in result.errors:
        table.add_row(f"[red]Hata: {slug}[/]", "—")

    console.print(table)

    stats = client.stats
    console.print(
        f"[dim]{stats.requests} istek · {stats.retries} yeniden deneme · "
        f"{stats.failures} başarısız · {stats.total_wait:.1f} sn hız sınırı beklemesi[/]"
    )

    if result.drops:
        drop_table = Table(title="Fiyat düşüşleri", header_style="bold green")
        drop_table.add_column("Ürün")
        drop_table.add_column("Site")
        drop_table.add_column("Önce", justify="right")
        drop_table.add_column("Sonra", justify="right")
        drop_table.add_column("Değişim", justify="right")

        for change in result.drops[:20]:
            percent = change.percent
            drop_table.add_row(
                change.product_title[:42],
                change.site_slug,
                f"{change.old_price} {change.currency or ''}",
                f"{change.new_price} {change.currency or ''}",
                f"{percent:.1f}%" if percent is not None else "—",
            )

        console.print(drop_table)

    for slug, message in result.errors.items():
        console.print(f"[yellow]Hata ({slug}):[/] {message}")


@app.command()
def products(
    limit: int = typer.Option(25, "--limit", "-n"),
    search: str = typer.Option(None, "--search", help="Başlıkta ara"),
) -> None:
    """Takip edilen ürünleri ve en düşük fiyatlarını listeler."""
    settings = Settings()

    async def _run():
        await init_db(settings.database_url)
        async with session_scope() as session:
            query = (
                select(
                    Product.id,
                    Product.title,
                    func.count(Offer.id),
                    func.min(Offer.last_price),
                    func.max(Offer.last_price),
                )
                .join(Offer, Offer.product_id == Product.id)
                .group_by(Product.id, Product.title)
                .order_by(Product.id.desc())
                .limit(limit)
            )
            if search:
                query = query.where(Product.title.ilike(f"%{search}%"))

            rows = (await session.execute(query)).all()

        await dispose_db()
        return rows

    rows = asyncio.run(_run())

    if not rows:
        console.print("Kayıtlı ürün yok. Önce `priceradar scrape` çalıştır.")
        return

    table = Table(header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("Ürün")
    table.add_column("Teklif", justify="right")
    table.add_column("En düşük", justify="right")
    table.add_column("En yüksek", justify="right")

    for product_id, title, offers, min_price, max_price in rows:
        table.add_row(
            str(product_id),
            title[:52],
            str(offers),
            f"{min_price}" if min_price is not None else "—",
            f"{max_price}" if max_price is not None else "—",
        )

    console.print(table)


@app.command()
def history(
    offer_id: int = typer.Argument(..., help="Teklif kimliği"),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Bir teklifin fiyat geçmişini gösterir."""
    settings = Settings()

    async def _run():
        await init_db(settings.database_url)
        async with session_scope() as session:
            offer = await session.get(Offer, offer_id)
            rows = (
                await session.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.offer_id == offer_id)
                    .order_by(PriceSnapshot.captured_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            title = offer.raw_title if offer else None
            snapshots = [
                (s.captured_at, s.price, s.currency, s.availability) for s in rows
            ]
        await dispose_db()
        return title, snapshots

    title, snapshots = asyncio.run(_run())

    if title is None:
        console.print(f"[bold red]Teklif bulunamadı:[/] {offer_id}")
        raise typer.Exit(code=1)

    table = Table(title=title[:60], header_style="bold cyan")
    table.add_column("Tarih")
    table.add_column("Fiyat", justify="right")
    table.add_column("Stok")

    for captured_at, price, currency, availability in snapshots:
        table.add_row(
            captured_at.strftime("%d.%m.%Y %H:%M"),
            f"{price} {currency or ''}" if price is not None else "—",
            availability,
        )

    console.print(table)


@app.command()
def runs(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """Geçmiş toplama çalıştırmalarını gösterir."""
    settings = Settings()

    async def _run():
        await init_db(settings.database_url)
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit)
                )
            ).scalars().all()
            data = [
                (r.id, r.started_at, r.items_found, r.products_created, r.price_changes, r.errors)
                for r in rows
            ]
        await dispose_db()
        return data

    data = asyncio.run(_run())

    if not data:
        console.print("Henüz çalıştırma yok.")
        return

    table = Table(header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("Tarih")
    table.add_column("Ürün", justify="right")
    table.add_column("Yeni", justify="right")
    table.add_column("Değişim", justify="right")
    table.add_column("Hata")

    for run_id, started, found, created, changes, errors in data:
        table.add_row(
            str(run_id),
            started.strftime("%d.%m.%Y %H:%M"),
            str(found),
            str(created),
            str(changes),
            (errors or "—")[:30],
        )

    console.print(table)


@app.command()
def robots(urls: list[str] = typer.Argument(None, help="Kontrol edilecek adresler")) -> None:
    """Sitelerin robots.txt durumunu gösterir.

    Bir site "yasak" diye atlanıyorsa önce bunu çalıştır: sorunun gerçek bir
    Disallow kuralı mı yoksa robots.txt'in indirilememesi mi olduğunu söyler.
    """
    settings = Settings()
    targets = urls or [c.start_urls[0] for c in load_site_configs(settings.sites_dir) if c.start_urls]

    async def _run():
        async with _build_client(settings) as client:
            results = []
            for url in targets:
                info = await client.robots_info(url)
                allowed, reason = await client.is_allowed(url)
                delay = await client.crawl_delay(url)
                results.append((url, info, allowed, reason, delay))
            return results

    results = asyncio.run(_run())

    table = Table(header_style="bold cyan")
    table.add_column("Adres")
    table.add_column("robots.txt")
    table.add_column("Sonuç")
    table.add_column("Crawl-delay")
    table.add_column("Gerekçe")

    for url, info, allowed, reason, delay in results:
        table.add_row(
            url[:46],
            str(info.status) if info.status else "ulaşılamadı",
            "[green]izinli[/]" if allowed else "[red]yasak[/]",
            f"{delay:.0f} sn" if delay else "—",
            f"{info.note} / {reason}"[:52],
        )

    console.print(table)


@app.command()
def version() -> None:
    """Sürüm bilgisi."""
    console.print(f"price-radar {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
