"""Veritabanı modelleri.

Şema tasarımının can alıcı noktası: **Product** ile **Offer** ayrı.

Bir "ürün" gerçek dünyadaki nesnedir (Sony WH-1000XM5). Bir "teklif" o ürünün
belirli bir sitedeki listelenmesidir. Aynı ürün beş sitede beş farklı URL ve
başlıkla durur. Bu ayrımı yapmazsan siteler arası fiyat karşılaştırması
yapamazsın — ki bu projenin asıl değeri orada.

PriceSnapshot her kontrolde değil, **fiyat veya stok değiştiğinde** yazılır.
Saatte bir çalışan bir sistemde aynı fiyatı 24 kez kaydetmenin anlamı yok.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    """Takip edilen bir kaynak site.

    Kazıma yapılandırması (başlangıç URL'leri, CSS seçicileri) burada tutuluyor.
    Böylece site eklemek için dosya düzenlemek gerekmiyor — API'den de yapılabilir.

    YAML dosyaları tohumlama mekanizması: açılışta veritabanına yazılırlar,
    ama tek doğruluk kaynağı veritabanıdır.
    """

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(500))
    adapter: Mapped[str] = mapped_column(String(64), default="css")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    max_pages: Mapped[int] = mapped_column(Integer, default=3)
    start_urls: Mapped[list] = mapped_column(JSON, default=list)
    selectors: Mapped[dict] = mapped_column(JSON, default=dict)
    pagination: Mapped[dict] = mapped_column(JSON, default=dict)
    browser: Mapped[dict] = mapped_column(JSON, default=dict)

    # "yaml" = dosyadan geldi, "api" = arayüzden eklendi.
    # YAML senkronizasyonu api ile eklenenlerin üzerine yazmasın diye gerekli.
    managed_by: Mapped[str] = mapped_column(String(10), default="yaml")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # cascade ORM seviyesinde de gerekli: ForeignKey'deki ondelete="CASCADE"
    # yalnızca veritabanı silmeyi kendisi yaptığında çalışır. ORM'den
    # session.delete(site) çağrıldığında SQLAlchemy varsayılan olarak
    # çocukların FK'sini NULL yapmaya çalışır ve NOT NULL kısıtına takılır.
    offers: Mapped[list["Offer"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Site {self.slug}>"


class Product(Base):
    """Gerçek dünyadaki ürün. Birden çok sitedeki teklifleri birleştirir."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offers: Mapped[list["Offer"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Product {self.title[:40]!r}>"


class Offer(Base):
    """Bir ürünün belirli bir sitedeki listelenmesi."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("site_id", "url", name="uq_offer_site_url"),
        Index("ix_offer_product_site", "product_id", "site_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))

    url: Mapped[str] = mapped_column(String(1000))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_title: Mapped[str] = mapped_column(String(500))

    # Son bilinen durum: her sorguda geçmişi taramak zorunda kalmamak için
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    last_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    last_availability: Mapped[str] = mapped_column(String(20), default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="offers")
    site: Mapped[Site] = relationship(back_populates="offers")
    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Offer {self.raw_title[:30]!r} @ {self.last_price}>"


class PriceSnapshot(Base):
    """Fiyat/stok değişikliği kaydı.

    Değişmediyse yazılmaz — geçmiş tablosu gereksiz satırla şişmesin.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snapshot_offer_time", "offer_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))

    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    availability: Mapped[str] = mapped_column(String(20), default="unknown")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    offer: Mapped[Offer] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<Snapshot {self.price} {self.currency} @ {self.captured_at:%Y-%m-%d}>"


class ScrapeRun(Base):
    """Bir toplama çalıştırması. Hata ayıklama ve raporlama için."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sites: Mapped[str | None] = mapped_column(String(500), nullable=True)

    items_found: Mapped[int] = mapped_column(Integer, default=0)
    products_created: Mapped[int] = mapped_column(Integer, default=0)
    offers_created: Mapped[int] = mapped_column(Integer, default=0)
    price_changes: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_merged: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ScrapeRun #{self.id} {self.items_found} ürün>"


class PriceAlert(Base):
    """Fiyat düştüğünde bildirim kuralı."""

    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))

    # Ya mutlak eşik ya yüzde düşüş
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    drop_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship()

    def __repr__(self) -> str:
        return f"<PriceAlert {self.email} ürün={self.product_id}>"
