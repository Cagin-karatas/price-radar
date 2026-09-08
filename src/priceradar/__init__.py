"""price-radar - cok siteli fiyat ve stok takip platformu."""

__version__ = "0.1.0"

from .matching import fingerprint, match, normalize_title
from .normalize import Availability, parse_availability, parse_price
from .pipeline import IngestResult, PriceChange, ingest_items, run_collection
from .scraping.base import ScrapedItem, SiteConfig

__all__ = [
    "__version__",
    "Availability",
    "IngestResult",
    "PriceChange",
    "ScrapedItem",
    "SiteConfig",
    "fingerprint",
    "ingest_items",
    "match",
    "normalize_title",
    "parse_availability",
    "parse_price",
    "run_collection",
]
