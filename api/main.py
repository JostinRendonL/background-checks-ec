"""
main.py — Background Checks Ecuador — FastAPI Server

Endpoints:
  GET  /health                     → healthcheck
  POST /consultar/bachiller        → verifica título de bachiller
  POST /consultar/satje            → verifica procesos judiciales
  POST /consultar/completo         → ambos módulos en paralelo
  POST /consultar/batch            → lista de cédulas → completo

Auth: header X-API-Key

Uso:
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Módulos de verificación
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.verificador_bachiller import consultar_cedula as verificar_bachiller
from src.verificador_satje import consultar_satje
from src.verificador_setec import consultar_setec
from src.verificador_fiscalia import consultar_fiscalia
from src.obs import init_sentry, capture_exception
from src.metrics import setup_metrics
from src.cache_redis import (
    cache_get, cache_set, cache_stats as redis_cache_stats,
    close as close_cache,
)

# Inicializar Sentry (opt-in con SENTRY_DSN) — antes de crear FastAPI
init_sentry(servicio="bg-api")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
API_KEY = os.getenv("BG_API_KEY", "dev-key-cambiar-en-produccion")
MAX_BATCH = int(os.getenv("MAX_BATCH_SIZE", "50"))
SEMAPHORE_CONCURRENCIA = int(os.getenv("SEMAPHORE", "3"))

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Background Checks Ecuador",
    description=(
        "API para verificación automática de antecedentes en Ecuador.\n\n"
        "Fuentes:\n"
        "- Ministerio de Educación (título de Bachiller)\n"
        "- Función Judicial (SATJE — procesos como actor y demandado)\n\n"
        "Devuelve un semáforo de riesgo (ROJO/AMARILLO/VERDE/GRIS)."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Métricas Prometheus opt-in (si METRICS_ENABLED=1)
setup_metrics(app)

# ── Auth ──────────────────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verificar_api_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="API Key inválida o ausente")
    return key

# ── Modelos ───────────────────────────────────────────────────────────────────
class ConsultaRequest(BaseModel):
    cedula: str = Field(..., description="Cédula ecuatoriana de 10 dígitos", example="0912345675")

class BatchRequest(BaseModel):
    cedulas: list[str] = Field(..., description=f"Lista de cédulas (máx {MAX_BATCH})", max_length=MAX_BATCH)

# ── Semáforo global de concurrencia ───────────────────────────────────────────
_semaphore = asyncio.Semaphore(SEMAPHORE_CONCURRENCIA)

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Sistema"])
async def health(deep: bool = False):
    """
    Healthcheck.
      /health         — liveness rápido (Docker HEALTHCHECK)
      /health?deep=1  — valida que Playwright pueda lanzar Chromium + SATJE responde
    """
    base = {
        "status":  "ok",
        "version": "1.2.0",
        "sentry":  "enabled" if os.getenv("SENTRY_DSN") else "disabled",
        "modulos": {
            "bachiller": "activo",
            "satje":     "activo",
            "setec":     "activo",
            "fiscalia":  "activo",
        },
        "concurrencia": SEMAPHORE_CONCURRENCIA,
    }
    if not deep:
        return base

    deps = {}

    # 1) Playwright — chromium puede arrancar?
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
        deps["playwright"] = {"status": "ok"}
    except Exception as e:
        deps["playwright"] = {"status": "down", "error": str(e)[:120]}

    # 2) SATJE upstream — responde el portal?
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=5.0, verify=False) as client:
            r = await client.get("https://procesosjudiciales.funcionjudicial.gob.ec/")
            deps["satje_upstream"] = {
                "status":    "ok" if r.status_code < 500 else "degraded",
                "http_code": r.status_code,
            }
    except Exception as e:
        deps["satje_upstream"] = {"status": "down", "error": str(e)[:120]}

    overall = "ok"
    for v in deps.values():
        if v.get("status") == "down":
            overall = "down"
            break
        if v.get("status") == "degraded" and overall == "ok":
            overall = "degraded"

    return {**base, "status": overall, "deps": deps}


async def _consultar_con_cache(tipo: str, cedula: str, ejecutar_fn) -> dict:
    """
    Helper genérico: chequea cache Redis → si hit devuelve. Si miss ejecuta
    la función real (con semaphore), guarda en cache, y devuelve.
    `ejecutar_fn` es una coroutine async sin args que hace el trabajo real.
    """
    # 1. Probar cache
    cached = await cache_get(tipo, cedula)
    if cached is not None:
        cached["_cache_hit"] = True
        cached["tiempo_seg"] = 0.0
        return cached

    # 2. Cache miss → ejecutar el scraper real
    t0 = time.time()
    async with _semaphore:
        resultado = await ejecutar_fn()
    resultado["tiempo_seg"] = round(time.time() - t0, 2)
    resultado["_cache_hit"] = False

    # 3. Guardar en cache (no bloquea si Redis está caído)
    await cache_set(tipo, cedula, resultado)
    return resultado


@app.post("/consultar/bachiller", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_bachiller_ep(req: ConsultaRequest):
    """Verifica título de bachiller en el Ministerio de Educación."""
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /bachiller → {cedula}")
    return await _consultar_con_cache(
        "bachiller", cedula,
        lambda: asyncio.to_thread(verificar_bachiller, cedula),
    )


@app.post("/consultar/satje", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_satje_ep(req: ConsultaRequest):
    """Verifica procesos judiciales en SATJE de la Función Judicial."""
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /satje → {cedula}")
    return await _consultar_con_cache(
        "satje", cedula,
        lambda: consultar_satje(cedula),
    )


@app.post("/consultar/setec", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_setec_ep(req: ConsultaRequest):
    """Verifica certificaciones de capacitación SETEC (Min. del Trabajo)."""
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /setec → {cedula}")
    return await _consultar_con_cache(
        "setec", cedula,
        lambda: consultar_setec(cedula),
    )


@app.post("/consultar/completo", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_completo_ep(req: ConsultaRequest):
    """
    Ejecuta bachiller + SATJE en paralelo. SETEC se consulta por separado
    via /consultar/setec para no bloquear el resultado principal.
    Aprovecha cache Redis por sub-resultado: si bachiller ya está cacheado,
    no se vuelve a scrapear (mismo con SATJE).
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /completo → {cedula}")
    t0 = time.time()

    # Wrapper: cache hit o ejecuta el scraper para cada uno
    async def get_bachiller():
        return await _consultar_con_cache(
            "bachiller", cedula,
            lambda: asyncio.to_thread(verificar_bachiller, cedula),
        )

    async def get_satje():
        return await _consultar_con_cache(
            "satje", cedula,
            lambda: consultar_satje(cedula),
        )

    resultados = await asyncio.gather(
        get_bachiller(), get_satje(), return_exceptions=True,
    )

    bachiller_res = resultados[0] if not isinstance(resultados[0], Exception) else {"estado": "ERROR", "detalle": str(resultados[0])}
    satje_res     = resultados[1] if not isinstance(resultados[1], Exception) else {"status": "ERROR", "detalle": str(resultados[1])}

    semaforo = _calcular_semaforo(bachiller_res, satje_res)

    return {
        "cedula":     cedula,
        "semaforo":   semaforo,
        "bachiller":  bachiller_res,
        "satje":      satje_res,
        "tiempo_seg": round(time.time() - t0, 2),
    }


@app.post("/consultar/fiscalia", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_fiscalia_ep(req: ConsultaRequest):
    """
    Consulta Noticias del Delito en el SIAF de la Fiscalia General del Estado.
    Detecta si la persona aparece como sospechoso/procesado en causas penales
    que aun no han llegado a juicio (y por lo tanto no aparecen en SATJE).
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /fiscalia -> {cedula}")
    return await _consultar_con_cache(
        "fiscalia", cedula,
        lambda: consultar_fiscalia(cedula),
    )


@app.get("/diagnostico/fiscalia", tags=["Diagnostico"], dependencies=[Depends(verificar_api_key)])
async def diagnostico_fiscalia_ep():
    """
    Diagnostico de 4 niveles del proxy + scraper Fiscalia.
    Devuelve qué pasa en cada nivel para identificar exactamente dónde falla.

    Test 1: httpx via proxy -> ipinfo.io (proxy basico OK?)
    Test 2: httpx via proxy -> SIAF homepage (sitio responde sin browser?)
    Test 3: Playwright via proxy -> ipinfo.io (Playwright + proxy OK?)
    Test 4: Playwright via proxy -> SIAF index.php, paso a paso (donde se cuelga?)
    """
    import httpx as _httpx
    from playwright.async_api import async_playwright as _pw

    proxy_url  = os.getenv("FISCALIA_PROXY_URL", "").strip()
    proxy_user = os.getenv("FISCALIA_PROXY_USER", "").strip()
    proxy_pass = os.getenv("FISCALIA_PROXY_PASS", "").strip()

    if not proxy_url:
        return {"error": "FISCALIA_PROXY_URL no configurado en env"}

    # Construir URL httpx con auth inline
    if proxy_user and proxy_pass:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        proxy_for_httpx = f"{parsed.scheme}://{proxy_user}:{proxy_pass}@{parsed.hostname}:{parsed.port}"
    else:
        proxy_for_httpx = proxy_url

    proxy_for_playwright = {"server": proxy_url}
    if proxy_user:
        proxy_for_playwright["username"] = proxy_user
    if proxy_pass:
        proxy_for_playwright["password"] = proxy_pass

    results: dict[str, dict] = {
        "proxy_configurado": {
            "server": proxy_url,
            "user":   proxy_user[:20] + "..." if len(proxy_user) > 20 else proxy_user,
            "tiene_pass": bool(proxy_pass),
        },
    }

    # ── Test 1: httpx via proxy a ipinfo.io ────────────────────────────────
    t0 = time.time()
    try:
        async with _httpx.AsyncClient(
            proxy=proxy_for_httpx,
            verify=False,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            r = await client.get("https://ipinfo.io/json")
            data = r.json()
        results["test_1_httpx_ipinfo"] = {
            "status":     "ok",
            "http_code":  r.status_code,
            "ip":         data.get("ip"),
            "country":    data.get("country"),
            "region":     data.get("region"),
            "org":        data.get("org"),
            "tiempo_seg": round(time.time() - t0, 2),
        }
    except Exception as e:
        results["test_1_httpx_ipinfo"] = {
            "status":     "fail",
            "error":      f"{type(e).__name__}: {str(e)[:200]}",
            "tiempo_seg": round(time.time() - t0, 2),
        }

    # ── Test 2: httpx via proxy a SIAF homepage ────────────────────────────
    t0 = time.time()
    try:
        async with _httpx.AsyncClient(
            proxy=proxy_for_httpx,
            verify=False,
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "es-EC,es;q=0.9",
            },
        ) as client:
            r = await client.get("https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php")
            body = r.text[:500]
        results["test_2_httpx_siaf"] = {
            "status":     "ok",
            "http_code":  r.status_code,
            "body_len":   len(r.text),
            "has_pwd":    "id=\"pwd\"" in r.text or "id='pwd'" in r.text,
            "incapsula":  "incapsula" in r.text.lower() or "_incap_" in r.text.lower(),
            "body_head":  body,
            "tiempo_seg": round(time.time() - t0, 2),
        }
    except Exception as e:
        results["test_2_httpx_siaf"] = {
            "status":     "fail",
            "error":      f"{type(e).__name__}: {str(e)[:200]}",
            "tiempo_seg": round(time.time() - t0, 2),
        }

    # ── Test 3: Playwright via proxy a ipinfo.io ───────────────────────────
    t0 = time.time()
    try:
        async with _pw() as p:
            browser = await p.chromium.launch(
                headless=True,
                proxy=proxy_for_playwright,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(ignore_https_errors=True)
            page = await ctx.new_page()
            await page.goto("https://ipinfo.io/json", wait_until="domcontentloaded", timeout=30_000)
            content = await page.content()
            await browser.close()
        results["test_3_playwright_ipinfo"] = {
            "status":     "ok",
            "body_len":   len(content),
            "body_head":  content[:300],
            "tiempo_seg": round(time.time() - t0, 2),
        }
    except Exception as e:
        results["test_3_playwright_ipinfo"] = {
            "status":     "fail",
            "error":      f"{type(e).__name__}: {str(e)[:300]}",
            "tiempo_seg": round(time.time() - t0, 2),
        }

    # ── Test 4: Playwright via proxy a SIAF, paso a paso ───────────────────
    pasos = {}
    t0_total = time.time()
    try:
        async with _pw() as p:
            browser = await p.chromium.launch(
                headless=True,
                proxy=proxy_for_playwright,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                locale="es-EC",
                ignore_https_errors=True,
            )
            page = await ctx.new_page()

            # Paso 4a: goto con wait_until="commit" (solo headers)
            t = time.time()
            try:
                resp = await page.goto(
                    "https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php",
                    wait_until="commit",
                    timeout=30_000,
                )
                pasos["4a_goto_commit"] = {
                    "status":     "ok",
                    "http_code":  resp.status if resp else None,
                    "tiempo_seg": round(time.time() - t, 2),
                }
            except Exception as e:
                pasos["4a_goto_commit"] = {
                    "status":     "fail",
                    "error":      f"{type(e).__name__}: {str(e)[:200]}",
                    "tiempo_seg": round(time.time() - t, 2),
                }
                raise

            # Paso 4b: esperar #pwd hasta 45s
            t = time.time()
            try:
                await page.wait_for_selector("#pwd", state="visible", timeout=45_000)
                pasos["4b_wait_pwd"] = {
                    "status":     "ok",
                    "tiempo_seg": round(time.time() - t, 2),
                }
            except Exception as e:
                # Capturar HTML actual para entender qué llegó
                try:
                    body = await page.content()
                except Exception:
                    body = ""
                pasos["4b_wait_pwd"] = {
                    "status":     "fail",
                    "error":      f"{type(e).__name__}: {str(e)[:150]}",
                    "tiempo_seg": round(time.time() - t, 2),
                    "body_len":   len(body),
                    "incapsula":  "incapsula" in body.lower() or "_incap_" in body.lower(),
                    "body_head":  body[:500],
                }

            await browser.close()
        results["test_4_playwright_siaf"] = {
            "status":     "ok" if pasos.get("4b_wait_pwd", {}).get("status") == "ok" else "partial",
            "pasos":      pasos,
            "tiempo_seg": round(time.time() - t0_total, 2),
        }
    except Exception as e:
        results["test_4_playwright_siaf"] = {
            "status":     "fail",
            "error":      f"{type(e).__name__}: {str(e)[:200]}",
            "pasos":      pasos,
            "tiempo_seg": round(time.time() - t0_total, 2),
        }

    return results


@app.post("/consultar/batch", tags=["Batch"], dependencies=[Depends(verificar_api_key)])
async def consultar_batch_ep(req: BatchRequest):
    """
    Procesa una lista de cédulas con verificación completa.
    Máximo MAX_BATCH cédulas por request. Concurrencia controlada.
    """
    cedulas = [c.strip() for c in req.cedulas[:MAX_BATCH]]
    logger.info(f"[API] /batch → {len(cedulas)} cédulas")
    t0 = time.time()

    sem_batch = asyncio.Semaphore(SEMAPHORE_CONCURRENCIA)

    async def procesar_una(cedula: str) -> dict:
        async with sem_batch:
            try:
                bachiller_res, satje_res = await asyncio.gather(
                    asyncio.to_thread(verificar_bachiller, cedula),
                    consultar_satje(cedula),
                    return_exceptions=True,
                )
                if isinstance(bachiller_res, Exception):
                    bachiller_res = {"estado": "ERROR", "detalle": str(bachiller_res)}
                if isinstance(satje_res, Exception):
                    satje_res = {"status": "ERROR", "detalle": str(satje_res)}
                return {
                    "cedula":    cedula,
                    "semaforo":  _calcular_semaforo(bachiller_res, satje_res),
                    "bachiller": bachiller_res,
                    "satje":     satje_res,
                }
            except Exception as e:
                capture_exception("batch.procesar_una", e, extra={"cedula": cedula})
                return {"cedula": cedula, "semaforo": "ERROR", "error": str(e)}

    resultados = await asyncio.gather(*[procesar_una(c) for c in cedulas])

    return {
        "total":      len(cedulas),
        "tiempo_seg": round(time.time() - t0, 2),
        "resultados": list(resultados),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validar_cedula(cedula: str):
    if not cedula.isdigit() or len(cedula) != 10:
        raise HTTPException(
            status_code=422,
            detail=f"Cédula inválida: '{cedula}'. Debe ser 10 dígitos numéricos."
        )


def _calcular_semaforo(bachiller: dict, satje: dict) -> str:
    """
    ROJO     — procesos judiciales como demandado (alta prioridad)
    AMARILLO — sin título de bachiller O procesos solo como actor
    VERDE    — bachiller OK y sin procesos
    GRIS     — error en algún módulo (datos insuficientes)
    """
    satje_error    = satje.get("status") == "ERROR"
    bachiller_error = bachiller.get("estado") == "ERROR" or bachiller.get("status") == "ERROR"

    # ROJO tiene prioridad absoluta — aunque bachiller falle
    tiene_juicio_penal = satje.get("total_demandado", 0) > 0
    if tiene_juicio_penal:
        return "ROJO"

    # Sin datos suficientes para determinar riesgo
    if satje_error or bachiller_error:
        return "GRIS"

    sin_bachiller      = bachiller.get("estado") == "NO_ENCONTRADO" or bachiller.get("tiene_titulo") == False
    tiene_causas_actor = satje.get("total_actor", 0) > 0

    if sin_bachiller or tiene_causas_actor:
        return "AMARILLO"
    return "VERDE"


# ── Cache management ─────────────────────────────────────────────────────────

@app.get("/cache/stats", tags=["Sistema"], dependencies=[Depends(verificar_api_key)])
async def cache_stats_ep():
    """Estadísticas del cache Redis: total de keys, keys por tipo, memoria usada."""
    return await redis_cache_stats()


# ── Browser pool management ──────────────────────────────────────────────────

@app.get("/pool/stats", tags=["Sistema"], dependencies=[Depends(verificar_api_key)])
async def pool_stats_ep():
    """Estado del pool de browsers Chromium pre-calentados."""
    from src.verificador_fiscalia import get_fiscalia_pool
    return {"fiscalia": get_fiscalia_pool().stats()}


@app.on_event("startup")
async def startup_event():
    """Pre-calentar el pool de browsers para Fiscalía al arrancar."""
    if os.getenv("BROWSER_POOL_EAGER_START", "1") == "1":
        try:
            from src.verificador_fiscalia import get_fiscalia_pool
            logger.info("[STARTUP] Pre-calentando pool de browsers Fiscalia...")
            await get_fiscalia_pool().start()
            logger.info("[STARTUP] Pool Fiscalia listo")
        except Exception as e:
            logger.error(f"[STARTUP] Error inicializando pool: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cerrar conexiones limpias al apagar el servicio."""
    try:
        await close_cache()
    except Exception:
        pass
    try:
        from src.verificador_fiscalia import get_fiscalia_pool
        await get_fiscalia_pool().shutdown()
    except Exception:
        pass
