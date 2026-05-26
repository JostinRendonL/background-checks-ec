"""
cache_redis.py — Cache compartido de resultados via Redis.

Comparte resultados de scraping (Bachiller/SATJE/SETEC/Fiscalía) entre
todos los servicios que llamen a bg-api (verifica, cv-worker, futuros
clientes), para evitar re-scrapear la misma cédula múltiples veces.

Keys: f"bgcache:{tipo}:{cedula}"
Valor: JSON serializado del resultado completo

TTLs por tipo de consulta (segundos):
  bachiller  — 90 días (títulos no cambian)
  setec      — 30 días (certificaciones cambian poco)
  satje      — 7 días  (procesos pueden agregarse)
  fiscalia   — 7 días  (noticias pueden agregarse)

Si CACHE_ENABLED=0 o Redis no está disponible, las funciones cache_get/set
devuelven None / no hacen nada → el flujo sigue sin cache (defensa
para no romper el bg-api si Redis cae).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
_REDIS_URL      = os.getenv("REDIS_URL", "").strip()
_CACHE_ENABLED  = os.getenv("CACHE_ENABLED", "1").strip() == "1"

# TTLs en segundos
_TTL_BY_TYPE: dict[str, int] = {
    "bachiller":  90 * 24 * 3600,   # 90 días
    "setec":      30 * 24 * 3600,   # 30 días
    "satje":       7 * 24 * 3600,   # 7 días
    "fiscalia":    7 * 24 * 3600,   # 7 días
    "completo":    7 * 24 * 3600,   # 7 días (el más conservador)
}

_KEY_PREFIX = "bgcache"

# Cliente Redis lazy (se inicializa en primera llamada)
_redis_client = None
_client_inited = False


def _get_client():
    """Crea (o devuelve) el cliente Redis async. None si está deshabilitado."""
    global _redis_client, _client_inited

    if _client_inited:
        return _redis_client

    _client_inited = True

    if not _CACHE_ENABLED:
        logger.info("[CACHE] Deshabilitado por CACHE_ENABLED=0")
        return None

    if not _REDIS_URL:
        logger.info("[CACHE] Deshabilitado (REDIS_URL no configurado)")
        return None

    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            _REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=3.0,
        )
        logger.info(f"[CACHE] Cliente Redis inicializado -> {_REDIS_URL[:50]}")
    except Exception as e:
        logger.warning(f"[CACHE] No se pudo inicializar Redis ({e}); cache desactivado")
        _redis_client = None

    return _redis_client


def _make_key(tipo: str, cedula: str) -> str:
    return f"{_KEY_PREFIX}:{tipo}:{cedula}"


async def cache_get(tipo: str, cedula: str) -> dict[str, Any] | None:
    """
    Busca un resultado cacheado. Devuelve dict si hay hit, None si miss.
    Nunca tira excepción — falla silencioso para no romper el endpoint.
    """
    client = _get_client()
    if client is None:
        return None

    try:
        raw = await client.get(_make_key(tipo, cedula))
        if raw is None:
            return None
        data = json.loads(raw)
        logger.info(f"[CACHE HIT] {tipo}:{cedula}")
        return data
    except Exception as e:
        logger.warning(f"[CACHE GET] error en {tipo}:{cedula}: {e}")
        return None


async def cache_set(tipo: str, cedula: str, data: dict[str, Any]) -> None:
    """
    Guarda un resultado en cache con TTL por tipo. Falla silencioso.
    No cachea resultados con error explícito (queremos reintentarlos).
    """
    client = _get_client()
    if client is None:
        return

    # No cachear errores transitorios — queremos que el próximo intento reintente
    if data.get("error") or data.get("estado") == "ERROR":
        return

    try:
        ttl = _TTL_BY_TYPE.get(tipo, 7 * 24 * 3600)
        await client.setex(
            _make_key(tipo, cedula),
            ttl,
            json.dumps(data, ensure_ascii=False),
        )
        logger.info(f"[CACHE SET] {tipo}:{cedula} (TTL {ttl // 3600}h)")
    except Exception as e:
        logger.warning(f"[CACHE SET] error en {tipo}:{cedula}: {e}")


async def cache_invalidate(tipo: str, cedula: str) -> None:
    """Borra la entrada cacheada. Útil para forzar re-scrape."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(_make_key(tipo, cedula))
        logger.info(f"[CACHE INVALIDATE] {tipo}:{cedula}")
    except Exception as e:
        logger.warning(f"[CACHE INVALIDATE] error en {tipo}:{cedula}: {e}")


async def cache_stats() -> dict[str, Any]:
    """Stats del cache para el endpoint de diagnóstico."""
    client = _get_client()
    if client is None:
        return {"enabled": False, "reason": "REDIS_URL no configurado o CACHE_ENABLED=0"}

    try:
        info = await client.info(section="memory")
        dbsize = await client.dbsize()
        # Contar keys por tipo
        counts = {}
        for tipo in _TTL_BY_TYPE.keys():
            pattern = f"{_KEY_PREFIX}:{tipo}:*"
            cnt = 0
            async for _ in client.scan_iter(match=pattern, count=500):
                cnt += 1
            counts[tipo] = cnt
        return {
            "enabled":   True,
            "db_size":   dbsize,
            "keys_by_type": counts,
            "memory_used_human": info.get("used_memory_human", "?"),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)[:200]}


async def close():
    """Cierra el cliente Redis (llamar en shutdown)."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.close()
        except Exception:
            pass
        _redis_client = None
