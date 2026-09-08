"""API şemaları.

Veritabanı modellerini doğrudan döndürmek yerine ayrı şemalar kullanıyoruz:
iç şema değiştiğinde API sözleşmesi kırılmasın ve `managed_by` gibi iç
alanlar dışarı sızmasın.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SiteBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    base_url: str = Field(..., max_length=500)
    adapter: str = Field("css", max_length=64)
    currency: str | None = Field(None, min_length=3, max_length=3)
    max_pages: int = Field(3, ge=1, le=50)
    start_urls: list[str] = Field(default_factory=list)
    selectors: dict = Field(default_factory=dict)
    pagination: dict = Field(default_factory=dict)
    browser: dict = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class SiteCreate(SiteBase):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")

    @field_validator("selectors")
    @classmethod
    def require_item_selector(cls, value: dict) -> dict:
        # 'item' olmadan adaptör hiçbir şey bulamaz; hatayı kayıt anında ver,
        # kazıma sırasında değil.
        if not value.get("item"):
            raise ValueError("selectors.item zorunlu (ürün kartını seçen CSS ifadesi)")
        return value


class SiteUpdate(BaseModel):
    """Kısmi güncelleme: sadece gönderilen alanlar değişir."""

    name: str | None = None
    base_url: str | None = None
    adapter: str | None = None
    currency: str | None = None
    max_pages: int | None = Field(None, ge=1, le=50)
    start_urls: list[str] | None = None
    selectors: dict | None = None
    pagination: dict | None = None
    browser: dict | None = None
    enabled: bool | None = None


class SiteOut(SiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    managed_by: str
    created_at: datetime


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price: Decimal | None
    currency: str | None
    availability: str
    captured_at: datetime


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    site_slug: str
    url: str
    raw_title: str
    last_price: Decimal | None
    last_currency: str | None
    last_availability: str
    last_checked_at: datetime | None


class ProductSummary(BaseModel):
    """Liste görünümü: teklif sayısı ve fiyat aralığı."""

    id: int
    title: str
    brand: str | None
    offer_count: int
    min_price: Decimal | None
    max_price: Decimal | None
    currency: str | None

    @property
    def spread(self) -> Decimal | None:
        if self.min_price is None or self.max_price is None:
            return None
        return self.max_price - self.min_price


class ProductDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    brand: str | None
    category: str | None
    created_at: datetime
    offers: list[OfferOut] = Field(default_factory=list)


class PriceChangeOut(BaseModel):
    offer_id: int
    product_title: str
    site_slug: str
    url: str
    old_price: Decimal | None
    new_price: Decimal | None
    currency: str | None
    percent: float | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    finished_at: datetime | None
    sites: str | None
    items_found: int
    products_created: int
    offers_created: int
    price_changes: int
    duplicates_merged: int
    errors: str | None


class ScrapeRequest(BaseModel):
    slugs: list[str] | None = Field(
        None, description="Boş bırakılırsa etkin tüm siteler kazınır"
    )


class ScrapeAccepted(BaseModel):
    """Kazıma arka planda çalışır; istek hemen döner."""

    status: str
    detail: str
    slugs: list[str] | None = None


class JobStatus(BaseModel):
    id: str
    name: str
    next_run_at: datetime | None
    trigger: str


class SchedulerStatus(BaseModel):
    running: bool
    scrape_interval_minutes: int
    jobs: list[JobStatus]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    scheduler_running: bool
    sites_enabled: int
    products: int


class Page(BaseModel):
    """Sayfalama zarfı."""

    total: int
    limit: int
    offset: int


class AlertBase(BaseModel):
    email: str = Field(..., max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    target_price: Decimal | None = Field(None, gt=0, description="Bu fiyatın altına inerse")
    drop_percent: Decimal | None = Field(
        None, gt=0, le=99, description="Bu yüzde kadar ucuzlarsa"
    )
    active: bool = True


class AlertCreate(AlertBase):
    product_id: int

    @model_validator(mode="after")
    def require_one_condition(self) -> "AlertCreate":
        # field_validator varsayılan değerler için çalışmıyor: iki alan da
        # gönderilmezse hiç kontrol edilmezdi. Model doğrulayıcı her zaman çalışır.
        if self.target_price is None and self.drop_percent is None:
            raise ValueError("target_price veya drop_percent'ten en az biri gerekli")
        return self


class AlertUpdate(BaseModel):
    email: str | None = None
    target_price: Decimal | None = None
    drop_percent: Decimal | None = None
    active: bool | None = None


class AlertOut(AlertBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_title: str | None = None
    last_triggered_at: datetime | None
    created_at: datetime


class AlertTestOut(BaseModel):
    """Alarm e-postasının önizlemesi — SMTP ayarını denemeden görmek için."""

    subject: str
    html: str
