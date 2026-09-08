"""CSS seçici tabanlı adaptör (BeautifulSoup).

Statik HTML üreten sitelerin çoğu için kod yazmaya gerek yok: YAML'da
seçicileri tarif etmek yeterli.

    selectors:
      item: "article.product_pod"
      title: "h3 a@title"          # @ ile öznitelik okunur
      url: "h3 a@href"
      price: "p.price_color"
      availability: "p.instock"

Bu tasarımın amacı müşterinin kendi sitesini kod yazmadan ekleyebilmesi.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..normalize import clean_text, parse_availability, parse_price
from .base import Adapter, ScrapedItem, register

logger = logging.getLogger(__name__)


def extract(node, selector: str | None) -> str:
    """Seçiciyi uygular ve metin ya da öznitelik döndürür.

    "h3 a@href" -> a etiketinin href özniteliği
    "p.price"   -> elemanın metni
    """
    if not selector or node is None:
        return ""

    attribute = None
    if "@" in selector:
        selector, attribute = selector.rsplit("@", 1)
        selector = selector.strip()

    target = node.select_one(selector) if selector else node
    if target is None:
        return ""

    if attribute:
        value = target.get(attribute, "")
        # class gibi çoklu değerli öznitelikler liste döner
        if isinstance(value, list):
            value = " ".join(value)
        return clean_text(value)

    return clean_text(target.get_text(" ", strip=True))


@register
class CssAdapter(Adapter):
    """YAML'daki CSS seçicileriyle statik sayfaları ayrıştırır."""

    name = "css"

    async def scrape(self) -> list[ScrapedItem]:
        items: list[ScrapedItem] = []
        seen_urls: set[str] = set()

        for start_url in self.config.start_urls:
            async for html, page_url in self._pages(start_url):
                page_items = self.parse(html, page_url)
                for item in page_items:
                    if item.url in seen_urls:
                        continue
                    seen_urls.add(item.url)
                    items.append(item)

        return items

    async def _pages(self, start_url: str):
        """Sayfalama: bir sonraki sayfa bağlantısını izleyerek gezer."""
        url = start_url
        next_selector = self.config.pagination.get("next")

        for page_number in range(self.config.max_pages):
            html = await self.client.fetch_text(url)
            yield html, url

            if not next_selector:
                return

            soup = BeautifulSoup(html, "lxml")
            next_node = soup.select_one(next_selector)
            if not next_node:
                logger.debug("%s: sayfa %d sonuncu", self.config.slug, page_number + 1)
                return

            href = next_node.get("href")
            if not href:
                return

            url = urljoin(url, href)

    def parse(self, html: str, page_url: str) -> list[ScrapedItem]:
        """HTML'den ürünleri çıkarır. Ağ erişimi gerektirmez — test edilebilir."""
        selectors = self.config.selectors
        item_selector = selectors.get("item")
        if not item_selector:
            raise ValueError(f"{self.config.slug}: 'selectors.item' tanımlı değil")

        soup = BeautifulSoup(html, "lxml")
        results: list[ScrapedItem] = []

        for node in soup.select(item_selector):
            title = extract(node, selectors.get("title"))
            href = extract(node, selectors.get("url"))
            url = urljoin(page_url, href) if href else page_url

            price_text = extract(node, selectors.get("price"))
            price, currency = parse_price(price_text, self.config.currency)

            availability_text = extract(node, selectors.get("availability"))
            availability = parse_availability(availability_text)

            results.append(
                ScrapedItem(
                    site_slug=self.config.slug,
                    url=url,
                    title=title,
                    price=price,
                    currency=currency,
                    availability=availability,
                    brand=extract(node, selectors.get("brand")) or None,
                    category=extract(node, selectors.get("category")) or None,
                    external_id=extract(node, selectors.get("external_id")) or None,
                    image_url=(
                        urljoin(page_url, extract(node, selectors.get("image")))
                        if selectors.get("image")
                        else None
                    ),
                    raw={"price_text": price_text, "availability_text": availability_text},
                )
            )

        return results
