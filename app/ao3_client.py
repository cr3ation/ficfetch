"""Single shared HTTP client for all AO3 traffic.

Every request in the app funnels through AO3Client.get(), which holds one
asyncio.Lock across the whole request+retry cycle. That lock IS the global
rate limiter: searches and downloads serialize and stay polite no matter how
many browser tabs are open.
"""
import asyncio
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import Settings
from .events import EventBus


class AO3Error(Exception):
    pass


class RestrictedWorkError(Exception):
    pass


def _cloudflare_hint(status: int) -> str:
    """AO3 sits behind Cloudflare: 52x means Cloudflare couldn't get an answer from AO3."""
    if 520 <= status <= 527:
        return " (Cloudflare couldn't reach AO3 — AO3 is likely overloaded)"
    return ""


class AO3Client:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies={"view_adult": "true"},
            follow_redirects=True,
            timeout=settings.request_timeout,
        )

    async def get(
        self, url: str, *, params: dict | None = None, follow_redirects: bool = True
    ) -> httpx.Response:
        s = self._settings
        async with self._lock:
            for attempt in range(s.max_retries + 1):
                gap = random.uniform(s.min_delay, s.max_delay)
                wait = self._last_request + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    resp = await self._client.get(url, params=params, follow_redirects=follow_redirects)
                except httpx.HTTPError as exc:
                    self._last_request = time.monotonic()
                    if attempt >= s.max_retries:
                        raise AO3Error(f"Network error after {attempt + 1} attempts: {exc}") from exc
                    delay = min(10 * (2**attempt), 120)
                    self._bus.log("warning", f"Network error ({exc.__class__.__name__}), retrying in {int(delay)}s...")
                    await asyncio.sleep(delay)
                    continue

                self._last_request = time.monotonic()

                if resp.status_code == 429:
                    delay = self._retry_after_seconds(resp)
                    if delay is None:
                        delay = min(s.backoff_base * (2**attempt), s.backoff_cap)
                    delay += random.uniform(0, 5)
                    self._bus.log(
                        "warning",
                        f"AO3 rate limit (429). Waiting {int(delay)}s before retry "
                        f"(attempt {attempt + 1}/{s.max_retries})...",
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 500:
                    if attempt >= s.max_retries:
                        raise AO3Error(f"AO3 server error {resp.status_code} for {url}")
                    delay = min(10 * (2**attempt), 120)
                    self._bus.log(
                        "warning",
                        f"AO3 server error {resp.status_code}{_cloudflare_hint(resp.status_code)}, "
                        f"retrying in {int(delay)}s...",
                    )
                    await asyncio.sleep(delay)
                    continue

                # 2xx/3xx/4xx (incl. 404, which callers need for fallback logic)
                return resp

            raise AO3Error(f"Gave up after {s.max_retries} retries: {url}")

    def _retry_after_seconds(self, resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                return None
        return max(0.0, min(seconds, self._settings.retry_after_cap))

    async def close(self) -> None:
        await self._client.aclose()
