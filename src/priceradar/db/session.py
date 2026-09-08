"""Veritabani baglantisi.

SQLAlchemy'nin async motoru kullaniliyor. Ayni kod hem SQLite hem
PostgreSQL ile calisiyor: testler SQLite'ta hizli kosuyor, uretimde
docker-compose PostgreSQL veriyor.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_url(url: str) -> str:
    """Senkron surucu adlarini async karsiliklariyla degistirir.

    Kullanicilar aliskanlikla 'postgresql://' yaziyor; async motor
    'postgresql+asyncpg://' istiyor. Hatayi burada onlemek, kullaniciya
    surucu adi ezberletmekten iyi.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def get_engine(url: str, echo: bool = False) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(normalize_url(url), echo=echo, pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Once get_engine() cagrilmali")
    return _session_factory


async def init_db(url: str, echo: bool = False) -> AsyncEngine:
    """Tablolari olusturur.

    Not: bu gelistirme kolayligi. Uretimde sema degisikligi icin Alembic
    gerekir - yol haritasinda.
    """
    engine = get_engine(url, echo=echo)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Veritabani hazir: %s", url.split("@")[-1])
    return engine


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Islem sinirini yoneten oturum baglami."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
