"""Uygulama ayarlari ve site tanimlarini yukleme."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .scraping.base import SiteConfig

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Ortam degiskenlerinden okunan ayarlar.

    Sifreler ve baglanti dizeleri kod veya YAML icinde tutulmaz.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRICERADAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///priceradar.db"
    sites_dir: Path = Path("config/sites")

    # Scraping
    user_agent: str = (
        "price-radar/0.1 (+https://github.com/Cagin-karatas/price-radar) fiyat takip botu"
    )
    min_interval: float = Field(default=1.0, description="Ayni siteye istekler arasi asgari saniye")
    max_concurrency: int = 5
    max_retries: int = 3
    request_timeout: float = 25.0
    respect_robots: bool = True
    proxies: list[str] = Field(default_factory=list)

    # Eslestirme
    match_threshold: float = 0.82

    # Zamanlanmis kazima. 0 verirsen zamanlayici hic baslamaz.
    scrape_interval_minutes: int = 60

    # API
    api_title: str = "price-radar"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # Bildirim
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_sender: str = ""
    alerts_enabled: bool = False
    alert_cooldown_hours: int = 12

    echo_sql: bool = False


def load_site_configs(directory: Path | str) -> list[SiteConfig]:
    """config/sites/*.yaml dosyalarindan site tanimlarini okur."""
    directory = Path(directory)
    if not directory.exists():
        logger.warning("Site klasoru bulunamadi: %s", directory)
        return []

    configs: list[SiteConfig] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.error("%s okunamadi: %s", path.name, exc)
            continue

        data.setdefault("slug", path.stem)
        try:
            configs.append(SiteConfig.from_dict(data))
        except TypeError as exc:
            logger.error("%s gecersiz: %s", path.name, exc)

    return configs
