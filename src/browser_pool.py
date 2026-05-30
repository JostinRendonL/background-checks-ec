"""
browser_pool.py — Pool de browsers Chromium pre-calentados (async).

Mantiene N browsers Playwright tibios listos para usar, evitando el
overhead de 5-10 segundos del `pw.chromium.launch()` en cada request.

Cada request toma un browser del pool con `acquire()`, crea un
`BrowserContext` aislado (cookies/localStorage limpios) y lo libera
con `release()` cuando termina. El browser queda disponible para
el siguiente request.

Rotación automática: cada browser se recicla (kill + respawn) después
de N usos para:
  1. Obtener IP nueva del proxy rotativo (Decodo Residential)
  2. Evitar memory leaks de Chromium en sesiones largas

Uso:
    pool = BrowserPool(size=3, launch_kwargs={"headless": True, "proxy": {...}})
    await pool.start()

    browser = await pool.acquire()
    try:
        ctx = await browser.new_context(...)
        page = await ctx.new_page()
        ...
        await ctx.close()
    finally:
        await pool.release(browser)

    await pool.shutdown()
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import async_playwright as _pw_default, Browser, Playwright

logger = logging.getLogger(__name__)


class BrowserPool:
    """Pool de browsers Chromium async con auto-recycling.

    Por default usa playwright. Para Fiscalia (que necesita mejor stealth
    contra Imperva), pasar `pw_factory=patchright.async_playwright`."""

    def __init__(
        self,
        size: int = 3,
        launch_kwargs: dict[str, Any] | None = None,
        max_uses_per_browser: int = 50,
        name: str = "default",
        pw_factory=None,
    ):
        self._size = size
        self._launch_kwargs = launch_kwargs or {"headless": True}
        self._max_uses = max_uses_per_browser
        self._name = name
        self._pw_factory = pw_factory or _pw_default

        self._pw: Playwright | None = None
        self._queue: asyncio.Queue[Browser] | None = None
        self._use_counts: dict[int, int] = {}   # id(browser) -> uses
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def start(self) -> None:
        """Inicializa Playwright y arranca los N browsers. Idempotente."""
        async with self._init_lock:
            if self._initialized:
                return

            logger.info(f"[POOL:{self._name}] Iniciando pool de {self._size} browsers "
                        f"({'patchright' if self._pw_factory is not _pw_default else 'playwright'})...")
            self._pw = await self._pw_factory().start()
            self._queue = asyncio.Queue(maxsize=self._size)

            for i in range(self._size):
                try:
                    browser = await self._launch_one()
                    await self._queue.put(browser)
                    logger.info(f"[POOL:{self._name}] Browser {i+1}/{self._size} listo")
                except Exception as e:
                    logger.error(f"[POOL:{self._name}] Error al lanzar browser {i+1}: {e}")
                    # Continuamos: el pool queda con menos browsers pero funcional

            self._initialized = True
            logger.info(f"[POOL:{self._name}] Pool listo con {self._queue.qsize()} browsers")

    async def _launch_one(self) -> Browser:
        """Lanza un browser fresco con los kwargs del pool."""
        assert self._pw is not None
        browser = await self._pw.chromium.launch(**self._launch_kwargs)
        self._use_counts[id(browser)] = 0
        return browser

    async def acquire(self, timeout: float = 60.0) -> Browser:
        """
        Toma un browser del pool. Si el browser murió, lo reemplaza
        automáticamente antes de devolverlo.
        """
        if not self._initialized:
            await self.start()

        assert self._queue is not None

        # Esperar hasta `timeout` segundos por un browser libre
        try:
            browser = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[POOL:{self._name}] Timeout esperando browser ({timeout}s). Lanzando uno nuevo.")
            return await self._launch_one()

        # Health check: si el browser murió, lanzar uno nuevo
        if not browser.is_connected():
            logger.warning(f"[POOL:{self._name}] Browser desconectado, lanzando reemplazo")
            self._use_counts.pop(id(browser), None)
            try:
                await browser.close()
            except Exception:
                pass
            browser = await self._launch_one()

        return browser

    async def release(self, browser: Browser) -> None:
        """
        Devuelve el browser al pool. Si llegó al límite de usos o
        está desconectado, lo recicla (kill + respawn).
        """
        if not self._initialized or self._queue is None:
            try:
                await browser.close()
            except Exception:
                pass
            return

        bid = id(browser)
        self._use_counts[bid] = self._use_counts.get(bid, 0) + 1

        # Reciclar si llegó al límite de usos (para rotar IP del proxy)
        # o si el browser está desconectado
        needs_recycle = (
            not browser.is_connected()
            or self._use_counts[bid] >= self._max_uses
        )

        if needs_recycle:
            uses = self._use_counts.pop(bid, 0)
            logger.info(f"[POOL:{self._name}] Reciclando browser tras {uses} usos")
            try:
                await browser.close()
            except Exception:
                pass
            try:
                new_browser = await self._launch_one()
                await self._queue.put(new_browser)
            except Exception as e:
                logger.error(f"[POOL:{self._name}] Error al respawn browser: {e}")
        else:
            await self._queue.put(browser)

    async def shutdown(self) -> None:
        """Cierra todos los browsers y Playwright."""
        if not self._initialized:
            return

        logger.info(f"[POOL:{self._name}] Cerrando pool...")
        if self._queue is not None:
            while not self._queue.empty():
                try:
                    browser = self._queue.get_nowait()
                    try:
                        await browser.close()
                    except Exception:
                        pass
                except asyncio.QueueEmpty:
                    break

        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass

        self._initialized = False
        self._pw = None
        self._queue = None
        self._use_counts = {}

    def stats(self) -> dict[str, Any]:
        """Stats del pool para diagnóstico."""
        return {
            "name":         self._name,
            "size":         self._size,
            "initialized":  self._initialized,
            "available":    self._queue.qsize() if self._queue else 0,
            "in_use":       (self._size - self._queue.qsize()) if self._queue else 0,
            "max_uses_per_browser": self._max_uses,
            "use_counts":   dict(self._use_counts),
        }
