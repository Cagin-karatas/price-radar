"""Async HTTP istemcisi.

Dört sorumluluğu var ve dördü de scraping'i "çalışıyor" ile "üretimde
kullanılabilir" arasındaki farkı oluşturuyor:

  1. Alan adı başına hız sınırı — eşzamanlı çalışırken bile
  2. Üstel geri çekilmeli yeniden deneme + Retry-After'a uyma
  3. robots.txt kontrolü (RFC 9309 semantiği)
  4. Proxy desteği — havuzdan sırayla seçim

Eşzamanlılık `asyncio.Semaphore` ile sınırlanıyor; hız sınırı ise alan adı
başına ayrı kilitlerle uygulanıyor, yani iki farklı siteye aynı anda
gidilebilir ama aynı siteye arka arkaya gidilmez.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "price-radar/0.1 (+https://github.com/Cagin-karatas/price-radar) "
    "fiyat takip botu"
)
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """İstek kalıcı olarak başarısız oldu."""

    def __init__(self, message: str, status: int = 0, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RobotsDisallowed(FetchError):
    """robots.txt bu adresi yasaklıyor."""


@dataclass
class RobotsInfo:
    """Bir sitenin robots.txt durumu."""

    url: str
    status: int
    parser: RobotFileParser | None
    note: str


@dataclass
class FetchStats:
    """Teşhis için sayaçlar."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    robots_blocked: int = 0
    total_wait: float = 0.0
    by_host: dict[str, int] = field(default_factory=dict)


class DomainRateLimiter:
    """Alan adı başına asgari istek aralığı uygular.

    Her alan adının kendi kilidi var: farklı sitelere eşzamanlı gidilir,
    aynı siteye sıraya girilir.
    """

    def __init__(self, min_interval: float = 1.0, jitter: float = 0.3) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_call: dict[str, float] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def acquire(self, url: str) -> float:
        """Gerekiyorsa bekler; beklenen süreyi döndürür."""
        host = urlparse(url).netloc

        async with self._lock_for(host):
            last = self._last_call.get(host)
            waited = 0.0

            if last is not None:
                # Jitter: sabit aralıkla istek atmak bot imzasıdır
                target = self.min_interval + random.uniform(0, self.jitter)
                elapsed = time.monotonic() - last
                if elapsed < target:
                    waited = target - elapsed
                    await asyncio.sleep(waited)

            self._last_call[host] = time.monotonic()
            return waited


class ProxyPool:
    """Proxy havuzu. Boşsa doğrudan bağlanılır.

    Sırayla seçim yapıyor; bir proxy başarısız olursa işaretlenip atlanıyor.
    """

    def __init__(self, proxies: list[str] | None = None) -> None:
        self.proxies = list(proxies or [])
        self._cycle = itertools.cycle(self.proxies) if self.proxies else None
        self._failed: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.proxies)

    def next(self) -> str | None:
        if not self._cycle:
            return None
        for _ in range(len(self.proxies)):
            candidate = next(self._cycle)
            if candidate not in self._failed:
                return candidate
        # Hepsi başarısızsa listeyi sıfırla, belki geçiciydi
        self._failed.clear()
        return next(self._cycle)

    def mark_failed(self, proxy: str) -> None:
        self._failed.add(proxy)
        logger.warning("Proxy başarısız olarak işaretlendi: %s", proxy)


class AsyncScraperClient:
    """Nazik, dayanıklı async HTTP istemcisi."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = 1.0,
        max_concurrency: int = 5,
        timeout: float = 25.0,
        max_retries: int = 3,
        respect_robots: bool = True,
        proxies: list[str] | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots

        self.limiter = DomainRateLimiter(min_interval)
        self.proxy_pool = ProxyPool(proxies)
        self.stats = FetchStats()

        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._robots: dict[str, RobotsInfo] = {}
        self._robots_lock = asyncio.Lock()

        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=timeout,
            follow_redirects=follow_redirects,
            proxy=self.proxy_pool.next(),
        )

    async def __aenter__(self) -> "AsyncScraperClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- robots --------------------------------------------------------------

    async def robots_info(self, url: str) -> RobotsInfo:
        """robots.txt durumunu getirir (host başına önbellekli).

        RFC 9309: dosya 4xx dönüyorsa kural yok sayılır. Python'un yerleşik
        RobotFileParser'ı 401/403'ü "her şey yasak" kabul ediyor ve
        Cloudflare arkasındaki siteler bot'lara robots.txt için bile 403
        döndürdüğünden, izin veren siteler erişilmez görünüyor.
        """
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"

        async with self._robots_lock:
            if root in self._robots:
                return self._robots[root]

            robots_url = f"{root}/robots.txt"
            try:
                response = await self._client.get(robots_url)
            except httpx.HTTPError as exc:
                info = RobotsInfo(robots_url, 0, None, f"İndirilemedi: {exc}")
            else:
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                    info = RobotsInfo(robots_url, 200, parser, "Kurallar okundu")
                elif 400 <= response.status_code < 500:
                    info = RobotsInfo(
                        robots_url,
                        response.status_code,
                        None,
                        f"HTTP {response.status_code} — RFC 9309: kural yok sayılır",
                    )
                else:
                    info = RobotsInfo(
                        robots_url,
                        response.status_code,
                        None,
                        f"HTTP {response.status_code} — sunucu hatası, izin verildi",
                    )

            self._robots[root] = info
            return info

    async def is_allowed(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, "robots.txt kontrolü kapalı"

        info = await self.robots_info(url)
        if info.parser is None:
            return True, info.note

        allowed = info.parser.can_fetch(self.user_agent, url)
        return allowed, ("izin veriyor" if allowed else "bu yolu yasaklıyor")

    async def crawl_delay(self, url: str) -> float | None:
        """robots.txt'te Crawl-delay varsa onu döndürür."""
        info = await self.robots_info(url)
        if info.parser is None:
            return None
        try:
            return info.parser.crawl_delay(self.user_agent)
        except (AttributeError, TypeError):
            return None

    # -- istek ---------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """Tek bir sayfayı indirir. Hız sınırı ve yeniden deneme uygulanır."""
        allowed, reason = await self.is_allowed(url)
        if not allowed:
            self.stats.robots_blocked += 1
            raise RobotsDisallowed(f"robots.txt {reason}: {url}", url=url)

        # Site kendi bekleme süresini belirtmişse ona uy
        delay = await self.crawl_delay(url)
        if delay and delay > self.limiter.min_interval:
            self.limiter.min_interval = float(delay)
            logger.info("Crawl-delay uygulanıyor: %.1f sn (%s)", delay, url)

        host = urlparse(url).netloc
        last_error: Exception | None = None

        async with self._semaphore:
            for attempt in range(self.max_retries):
                waited = await self.limiter.acquire(url)
                self.stats.total_wait += waited
                self.stats.requests += 1
                self.stats.by_host[host] = self.stats.by_host.get(host, 0) + 1

                try:
                    response = await self._client.get(url, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = exc
                    self.stats.retries += 1
                    logger.warning(
                        "İstek hatası (%d/%d) %s: %s", attempt + 1, self.max_retries, url, exc
                    )
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                if response.status_code in RETRY_STATUSES:
                    self.stats.retries += 1
                    wait = self._retry_after(response) or self._backoff(attempt)
                    logger.warning(
                        "HTTP %d, %.1f sn sonra tekrar: %s", response.status_code, wait, url
                    )
                    last_error = FetchError(
                        f"HTTP {response.status_code}", response.status_code, url
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code >= 400:
                    self.stats.failures += 1
                    raise FetchError(
                        f"HTTP {response.status_code}: {url}", response.status_code, url
                    )

                return response

        self.stats.failures += 1
        raise FetchError(f"{url} alınamadı: {last_error}", url=url)

    async def fetch_text(self, url: str, **kwargs) -> str:
        response = await self.fetch(url, **kwargs)
        return response.text

    async def fetch_json(self, url: str, **kwargs):
        response = await self.fetch(url, **kwargs)
        return response.json()

    def _backoff(self, attempt: int) -> float:
        """Üstel geri çekilme + jitter (thundering herd'ü önler)."""
        return min(2**attempt + random.uniform(0, 1), 30.0)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(float(raw), 60.0)
        except ValueError:
            return None
