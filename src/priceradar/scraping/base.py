"""Adaptör arayüzü ve kayıt defteri.

Her site farklı HTML üretir. Adaptör, o farklılığı tek bir yerde tutar:
boru hattının geri kalanı `ScrapedItem` listesinden başka bir şey görmez.

Üç adaptör türü var:
  - `css`    : YAML'daki CSS seçicileriyle statik HTML (kod yazmadan site eklenir)
  - `browser`: JavaScript ile üretilen sayfalar (Playwright)
  - özel     : bu sınıftan türetilen Python sınıfları (API'ler, tuhaf şemalar)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from ..normalize import Availability

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type["Adapter"]] = {}


@dataclass
class ScrapedItem:
    """Bir siteden çıkarılmış tek ürün listelemesi.

    Henüz veritabanı nesnesi değil — ham ama normalleştirilmiş veri.
    """

    site_slug: str
    url: str
    title: str
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    brand: str | None = None
    category: str | None = None
    external_id: str | None = None
    image_url: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Kaydedilmeye değer mi?

        Başlığı ve URL'si olmayan bir kayıt işe yaramaz. Fiyatı olmayan
        kayıt tutulur — stokta olmayan ürünün fiyatı gösterilmez, bu normaldir.
        """
        return bool(self.title.strip()) and bool(self.url.strip())

    def __repr__(self) -> str:
        return f"<ScrapedItem {self.title[:35]!r} {self.price} {self.currency}>"


@dataclass
class SiteConfig:
    """Bir sitenin YAML'dan okunan tanımı."""

    slug: str
    name: str
    base_url: str
    adapter: str = "css"
    start_urls: list[str] = field(default_factory=list)
    currency: str | None = None
    enabled: bool = True
    max_pages: int = 3
    selectors: dict = field(default_factory=dict)
    pagination: dict = field(default_factory=dict)
    browser: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "SiteConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def register(cls: type["Adapter"]) -> type["Adapter"]:
    """Adaptörü adıyla kaydeder."""
    _REGISTRY[cls.name] = cls
    return cls


def available_adapters() -> dict[str, type["Adapter"]]:
    return dict(_REGISTRY)


def build_adapter(config: SiteConfig, client) -> "Adapter":
    if config.adapter not in _REGISTRY:
        raise KeyError(
            f"Bilinmeyen adaptör: {config.adapter!r}. "
            f"Seçenekler: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[config.adapter](config, client)


class Adapter(ABC):
    """Tüm site adaptörlerinin ortak arayüzü."""

    name: str = "base"

    def __init__(self, config: SiteConfig, client) -> None:
        self.config = config
        self.client = client

    @abstractmethod
    async def scrape(self) -> list[ScrapedItem]:
        """Siteyi gezer ve ürünleri döndürür."""

    async def safe_scrape(self) -> tuple[list[ScrapedItem], Exception | None]:
        """Hata fırlatmayan sürüm: bir site çöktüğünde diğerleri devam etsin."""
        try:
            items = await self.scrape()
        except Exception as exc:  # noqa: BLE001 — kasıtlı geniş yakalama
            logger.error("%s başarısız: %s", self.config.slug, exc)
            return [], exc

        valid = [item for item in items if item.is_valid]
        dropped = len(items) - len(valid)
        if dropped:
            logger.warning("%s: %d geçersiz kayıt atlandı", self.config.slug, dropped)

        logger.info("%s: %d ürün alındı", self.config.slug, len(valid))
        return valid, None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.config.slug}>"
