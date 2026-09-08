"""Playwright adaptörü — JavaScript ile üretilen sayfalar için.

Playwright isteğe bağlı bir bağımlılık: kurulu değilse çekirdek çalışmaya
devam eder, sadece bu adaptör kullanılamaz. Tarayıcı indirmek 300 MB'ın
üzerinde, herkesin buna ihtiyacı yok.

    pip install "price-radar[browser]"
    playwright install chromium

Ayrıştırma mantığı CssAdapter ile aynı: tarayıcı yalnızca HTML'i üretmek
için kullanılıyor. Böylece `parse()` testleri tarayıcı olmadan çalışıyor.
"""

from __future__ import annotations

import logging

from .base import ScrapedItem, register
from .css_adapter import CssAdapter

logger = logging.getLogger(__name__)


class PlaywrightUnavailable(RuntimeError):
    """Playwright kurulu değil."""


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - ortama bağlı
        raise PlaywrightUnavailable(
            "Playwright kurulu değil. Kurmak için:\n"
            '  pip install "price-radar[browser]"\n'
            "  playwright install chromium"
        ) from exc
    return async_playwright


@register
class BrowserAdapter(CssAdapter):
    """Sayfayı gerçek bir tarayıcıda açar, JS çalıştıktan sonra HTML'i alır.

    YAML'da ayarlanabilenler:

        browser:
          wait_for: ".product-card"    # bu eleman görünene kadar bekle
          wait_ms: 1500                # ek bekleme
          scroll: true                 # sonsuz kaydırma için sayfayı kaydır
          headless: true
    """

    name = "browser"

    async def scrape(self) -> list[ScrapedItem]:
        async_playwright = _import_playwright()

        options = self.config.browser or {}
        headless = options.get("headless", True)
        wait_for = options.get("wait_for")
        wait_ms = int(options.get("wait_ms", 0))
        scroll = bool(options.get("scroll", False))

        items: list[ScrapedItem] = []
        seen: set[str] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(
                user_agent=self.client.user_agent,
                locale=options.get("locale", "en-US"),
            )
            page = await context.new_page()

            try:
                for start_url in self.config.start_urls:
                    # robots.txt kontrolü tarayıcı yolunda da geçerli
                    allowed, reason = await self.client.is_allowed(start_url)
                    if not allowed:
                        logger.warning("robots.txt %s: %s", reason, start_url)
                        continue

                    await self.client.limiter.acquire(start_url)
                    await page.goto(start_url, wait_until="domcontentloaded")

                    if wait_for:
                        try:
                            await page.wait_for_selector(wait_for, timeout=15_000)
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "%s: '%s' beklenirken zaman aşımı", self.config.slug, wait_for
                            )

                    if scroll:
                        await self._scroll_to_bottom(page)

                    if wait_ms:
                        await page.wait_for_timeout(wait_ms)

                    html = await page.content()
                    for item in self.parse(html, page.url):
                        if item.url not in seen:
                            seen.add(item.url)
                            items.append(item)
            finally:
                await context.close()
                await browser.close()

        return items

    @staticmethod
    async def _scroll_to_bottom(page, max_steps: int = 10) -> None:
        """Sonsuz kaydırmalı sayfalarda içerik yüklenene kadar aşağı iner."""
        previous_height = 0
        for _ in range(max_steps):
            height = await page.evaluate("document.body.scrollHeight")
            if height == previous_height:
                return
            previous_height = height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
