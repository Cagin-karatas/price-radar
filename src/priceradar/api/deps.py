"""FastAPI bagimliliklari."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import Annotated

from ..config import Settings
from ..db.session import get_session_factory


@lru_cache
def get_settings() -> Settings:
    """Ayarlar surec boyunca sabit; her istekte .env okumaya gerek yok."""
    return Settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    """Istek basina veritabani oturumu.

    Islem siniri burada: istek basariyla biterse commit, hata olursa rollback.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
