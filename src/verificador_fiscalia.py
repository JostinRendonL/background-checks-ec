"""
verificador_fiscalia.py — Consulta Noticias del Delito (SIAF Fiscalias Ecuador)

Portal: https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php
Tecnologia: PHP + JS. Protegido por Incapsula -> bypasseable con Playwright + stealth.

Flujo:
  1. Navegar al portal (stealth para pasar Incapsula)
  2. Llenar #pwd con la cedula
  3. Click #btn_buscar_denuncia
  4. Parsear tabla de resultados: noticias del delito + roles del sujeto

Roles que generan alerta: SOSPECHOSO, IMPUTADO, PROCESADO, ACUSADO, SENTENCIADO
Roles neutros (no generan alerta): DENUNCIANTE, VICTIMA, TESTIGO, OFENDIDO
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx
# Patchright (fork de Playwright con stealth fuerte built-in) — mejor contra Imperva
# Si no esta instalado, fallback a playwright normal (API es identica)
try:
    from patchright.async_api import async_playwright, TimeoutError as PWTimeout
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    _USING_PATCHRIGHT = False

try:
    from playwright_stealth import stealth_async
    _HAS_STEALTH_LIB = True
except ImportError:
    _HAS_STEALTH_LIB = False

# Pool de browsers (singleton, inicializado lazy desde get_fiscalia_pool)
from src.browser_pool import BrowserPool

_fiscalia_pool: BrowserPool | None = None


def get_fiscalia_pool() -> BrowserPool:
    """
    Devuelve el pool singleton de browsers para Fiscalía.
    Lazy init: la primera llamada construye el pool con la config del proxy
    actual. start() se llama en api/main.py startup_event.
    """
    global _fiscalia_pool
    if _fiscalia_pool is None:
        proxy_cfg = _build_proxy_cfg()
        size = int(os.getenv("FISCALIA_POOL_SIZE", "3"))
        launch_kwargs = {
            "headless": True,
            "proxy":    proxy_cfg,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--lang=es-EC",
            ],
        }
        _fiscalia_pool = BrowserPool(
            size=size,
            launch_kwargs=launch_kwargs,
            max_uses_per_browser=25,   # rota IP de Decodo cada 25 usos
            name="fiscalia",
            # Si patchright esta disponible, lo usamos para mejor stealth contra Imperva
            pw_factory=(async_playwright if _USING_PATCHRIGHT else None),
        )
    return _fiscalia_pool

logger = logging.getLogger(__name__)

_URL         = "https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php"
_TIMEOUT_NAV    = 75_000   # navegacion principal (proxy residencial es lento)
_TIMEOUT_WARM   = 12_000   # warming homepage (no critico, falla silencioso)
_TIMEOUT_RES    = 18_000
_SLEEP_INIT     = 3.0
_SLEEP_CLICK    = 4.0

# ── Estrategia de conexion (orden de preferencia) ────────────────────────────
# 1. Browser API (Bright Data Scraping Browser) — wss://... con anti-bot integrado
# 2. Proxy residencial — http(s):// con stealth manual
# 3. Sin proxy — solo para dev local
_BROWSER_API_URL = os.getenv("FISCALIA_BROWSER_API_URL", "").strip()

_PROXY_URL  = os.getenv("FISCALIA_PROXY_URL", "").strip()
_PROXY_USER = os.getenv("FISCALIA_PROXY_USER", "").strip()
_PROXY_PASS = os.getenv("FISCALIA_PROXY_PASS", "").strip()

# ── Pool de proxies (rotacion entre reintentos) ──────────────────────────────
# Soporta 2 formas de configurar (ambas opcionales — fallback al proxy unico):
#   1) FISCALIA_PROXY_LIST_FILE = /app/proxies.csv
#      Una linea por proxy: host:port:user:pass
#   2) FISCALIA_PROXY_LIST = "host1:port:user:pass,host2:port:user:pass,..."
#      Comma-separated en el mismo formato.
#
# En cada llamada se elige un proxy al azar; si falla, el siguiente intento
# excluye el ya usado (asi 10 proxies = 10 IPs distintas en reintentos).

import random
_PROXY_LIST_FILE = os.getenv("FISCALIA_PROXY_LIST_FILE", "").strip()
_PROXY_LIST_ENV  = os.getenv("FISCALIA_PROXY_LIST", "").strip()


def _cargar_pool_proxies() -> list[dict]:
    """Carga el pool de proxies desde archivo o env var. Cada proxy es un dict
    {server, username, password} listo para Playwright. Log verbose para debug."""
    logger.info(f"[FISCALIA] Cargando pool de proxies...")
    logger.info(f"[FISCALIA]   FISCALIA_PROXY_LIST_FILE = '{_PROXY_LIST_FILE}'")
    logger.info(f"[FISCALIA]   FISCALIA_PROXY_LIST      = {'(set, ' + str(len(_PROXY_LIST_ENV)) + ' chars)' if _PROXY_LIST_ENV else '(not set)'}")

    raw_lines: list[str] = []
    if _PROXY_LIST_FILE:
        if os.path.exists(_PROXY_LIST_FILE):
            try:
                with open(_PROXY_LIST_FILE, "r", encoding="utf-8") as f:
                    raw_lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                logger.info(f"[FISCALIA]   Archivo leido: {_PROXY_LIST_FILE} ({len(raw_lines)} lineas)")
            except Exception as e:
                logger.error(f"[FISCALIA]   ERROR leyendo {_PROXY_LIST_FILE}: {e}")
        else:
            logger.warning(f"[FISCALIA]   ARCHIVO NO EXISTE: {_PROXY_LIST_FILE}")
    elif _PROXY_LIST_ENV:
        raw_lines = [s.strip() for s in _PROXY_LIST_ENV.split(",") if s.strip()]
        logger.info(f"[FISCALIA]   Lista cargada de env var ({len(raw_lines)} entradas)")
    else:
        logger.info(f"[FISCALIA]   Ninguna fuente de pool configurada — usando FISCALIA_PROXY_URL si existe")

    pool = []
    for line in raw_lines:
        parts = line.split(":")
        if len(parts) < 2:
            logger.warning(f"[FISCALIA]   Linea invalida (necesita host:port[:user:pass]): {line[:30]}...")
            continue
        host, port = parts[0], parts[1]
        user = parts[2] if len(parts) > 2 else ""
        passw = ":".join(parts[3:]) if len(parts) > 3 else ""
        cfg = {"server": f"http://{host}:{port}"}
        if user:  cfg["username"] = user
        if passw: cfg["password"] = passw
        pool.append(cfg)
    return pool


# Pool lazy: se carga en la primera llamada (cuando logging ya esta configurado).
# Si lo cargamos a nivel de modulo, los logs.info() se pierden porque
# api/main.py hace el import ANTES de logging.basicConfig().
_PROXY_POOL: list[dict] | None = None  # None = no inicializado todavia

def _obtener_pool() -> list[dict]:
    """Devuelve el pool. Lo carga la primera vez (lazy)."""
    global _PROXY_POOL
    if _PROXY_POOL is None:
        _PROXY_POOL = _cargar_pool_proxies()
        if _PROXY_POOL:
            logger.info(f"[FISCALIA] ✓ Pool de proxies cargado: {len(_PROXY_POOL)} IPs disponibles para rotacion")
        else:
            logger.info(f"[FISCALIA] Pool vacio. Usara FISCALIA_PROXY_URL='{_PROXY_URL or '(none)'}' como fallback.")
    return _PROXY_POOL


def _build_proxy_cfg(excluir: list[str] | None = None) -> dict | None:
    """Devuelve un proxy para Playwright.
    - Si hay POOL: elige uno random, excluyendo los servers ya intentados.
    - Si no hay POOL pero hay FISCALIA_PROXY_URL: usa ese (modo viejo).
    - Si nada esta configurado: None (directo, solo dev local).
    """
    excluir = excluir or []
    # Pool tiene prioridad
    pool = _obtener_pool()
    if pool:
        candidatos = [p for p in pool if p["server"] not in excluir]
        if not candidatos:
            # Todos los del pool fallaron en este intento — reusar random
            candidatos = pool
        return random.choice(candidatos)
    # Fallback al proxy unico
    if not _PROXY_URL:
        return None
    cfg: dict[str, str] = {"server": _PROXY_URL}
    if _PROXY_USER:
        cfg["username"] = _PROXY_USER
    if _PROXY_PASS:
        cfg["password"] = _PROXY_PASS
    return cfg


# ── 2captcha — resolver hCaptcha del portal de Fiscalia ─────────────────
# El portal cuando detecta IP "sospechosa" sirve un hCaptcha (CAPTCHA visual).
# 2captcha lo resuelve por ~$0.0025 por captcha (humanos reales).
_TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_API_KEY", "").strip()


async def _resolver_hcaptcha(sitekey: str, page_url: str) -> str | None:
    """Resuelve un hCaptcha via 2captcha. Devuelve el token o None si falla.
    Tiempo tipico: 15-45 segundos (humanos resolviendo)."""
    if not _TWOCAPTCHA_KEY:
        logger.warning("[FISCALIA] hCaptcha detectado pero TWOCAPTCHA_API_KEY no configurada")
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # 1) Enviar tarea
            r = await client.post(
                "https://2captcha.com/in.php",
                data={
                    "key":     _TWOCAPTCHA_KEY,
                    "method":  "hcaptcha",
                    "sitekey": sitekey,
                    "pageurl": page_url,
                    "json":    "1",
                },
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") != 1:
                logger.warning(f"[FISCALIA] 2captcha rechazo el envio: {data.get('request')}")
                return None
            task_id = data["request"]
            logger.info(f"[FISCALIA] hCaptcha enviado a 2captcha (task {task_id}), esperando...")

            # 2) Polling (max 90s, cada 5s)
            for intento in range(18):
                await asyncio.sleep(5)
                r2 = await client.get(
                    "https://2captcha.com/res.php",
                    params={"key": _TWOCAPTCHA_KEY, "action": "get",
                            "id": task_id, "json": "1"},
                )
                r2.raise_for_status()
                d2 = r2.json()
                if d2.get("status") == 1:
                    token = (d2.get("request") or "").strip()
                    logger.info(f"[FISCALIA] hCaptcha resuelto en {(intento+1)*5}s "
                                f"(token len={len(token)})")
                    return token
                req = d2.get("request", "")
                if req != "CAPCHA_NOT_READY":
                    logger.warning(f"[FISCALIA] 2captcha error: {req}")
                    return None
            logger.warning("[FISCALIA] 2captcha timeout (90s)")
            return None
    except Exception as e:
        logger.warning(f"[FISCALIA] _resolver_hcaptcha fallo: {e}")
        return None


async def _detectar_y_resolver_hcaptcha(page) -> bool:
    """Inspecciona la pagina; si hay hCaptcha, lo resuelve via 2captcha
    e inyecta el token. Devuelve True si resolvio (o no habia captcha)."""
    try:
        # Buscar el iframe/div del hCaptcha y extraer sitekey
        sitekey = await page.evaluate("""() => {
            // Buscar en multiples lugares donde puede estar el sitekey
            const el1 = document.querySelector('[data-sitekey]');
            if (el1) return el1.getAttribute('data-sitekey');
            const iframe = document.querySelector('iframe[src*="hcaptcha.com"]');
            if (iframe) {
                const m = iframe.src.match(/sitekey=([\\w-]+)/);
                if (m) return m[1];
            }
            return null;
        }""")
        if not sitekey:
            return True  # no hay captcha visible
        logger.info(f"[FISCALIA] hCaptcha detectado (sitekey={sitekey[:8]}...)")
        page_url = page.url
        token = await _resolver_hcaptcha(sitekey, page_url)
        if not token:
            return False
        # Inyectar el token: textarea de h-captcha-response + dispatch event
        await page.evaluate(f"""(token) => {{
            const ta = document.querySelector('[name="h-captcha-response"]')
                    || document.querySelector('textarea[name="h-captcha-response"]');
            if (ta) {{ ta.value = token; ta.innerHTML = token; }}
            // A veces tambien hay un g-recaptcha-response shim
            const ga = document.querySelector('[name="g-recaptcha-response"]');
            if (ga) {{ ga.value = token; ga.innerHTML = token; }}
            // Disparar callback de hCaptcha si existe
            try {{
                if (window.hcaptcha && typeof window.hcaptcha.execute === 'function') {{
                    // nada, ya tenemos token
                }}
                // Algunos sitios escuchan submit del form despues del token
            }} catch(e) {{}}
        }}""", token)
        logger.info("[FISCALIA] Token de hCaptcha inyectado en DOM")
        await asyncio.sleep(1)
        return True
    except Exception as e:
        logger.warning(f"[FISCALIA] _detectar_y_resolver_hcaptcha fallo: {e}")
        return False


_STEALTH_JS = """
    // ── navigator props ─────────────────────────────────────────────
    Object.defineProperty(navigator, 'webdriver',          {get: () => undefined});
    Object.defineProperty(navigator, 'plugins',            {get: () => [
        {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
        {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
        {name:'Native Client',     filename:'internal-nacl-plugin'}
    ]});
    Object.defineProperty(navigator, 'languages',          {get: () => ['es-EC','es','en-US','en']});
    Object.defineProperty(navigator, 'platform',           {get: () => 'Win32'});
    Object.defineProperty(navigator, 'hardwareConcurrency',{get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory',       {get: () => 8});
    Object.defineProperty(navigator, 'maxTouchPoints',     {get: () => 0});
    Object.defineProperty(navigator, 'vendor',             {get: () => 'Google Inc.'});

    // ── chrome runtime stub ─────────────────────────────────────────
    window.chrome = {runtime:{}, loadTimes:function(){}, csi:function(){}, app:{}};

    // ── WebGL vendor/renderer (anti-fingerprint) ────────────────────
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';                          // UNMASKED_VENDOR_WEBGL
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';            // UNMASKED_RENDERER_WEBGL
        return getParameter.apply(this, [parameter]);
    };

    // ── permissions API consistencia ────────────────────────────────
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : originalQuery(parameters);
"""

_ROLES_SOSPECHOSO = {
    "SOSPECHOSO", "SOSPECHOSA",
    "IMPUTADO", "IMPUTADA",
    "PROCESADO", "PROCESADA",
    "ACUSADO", "ACUSADA",
    "SENTENCIADO", "SENTENCIADA",
    "INVESTIGADO", "INVESTIGADA",
}
_ROLES_NEUTROS = {
    "DENUNCIANTE",
    "VICTIMA", "VÍCTIMA",
    "OFENDIDO", "OFENDIDA",
    "TESTIGO",
    "AGRAVIADO", "AGRAVIADA",
    "PERJUDICADO", "PERJUDICADA",   # ← NUEVO (caso de estafa)
    "AFECTADO", "AFECTADA",         # ← preventivo
}


_MAX_INTENTOS = 3   # Bright Data rota IPs; reintentar con nueva sesion = nueva IP
_TIMEOUT_PWD_INICIAL = 12_000   # primer intento al form (rapido si la IP esta limpia)
_TIMEOUT_PWD_RELOAD  = 18_000   # segundo intento despues del reload (post-challenge)
# Con Browser API el bypass es mucho mejor: menos intentos necesarios + form aparece directo
_TIMEOUT_PWD_BROWSER_API = 45_000   # Browser API resuelve challenges internamente


async def consultar_fiscalia(cedula: str) -> dict[str, Any]:
    """
    Consulta la cedula en el SIAF de la Fiscalia General del Estado.

    Estrategia:
    - Si FISCALIA_BROWSER_API_URL esta seteado -> usa Bright Data Browser API
      (Scraping Browser remoto con anti-bot integrado, max 2 intentos).
    - Si no -> usa proxy residencial + Playwright local con stealth
      (hasta _MAX_INTENTOS intentos con browsers nuevos para rotar IP).
    """
    cedula = (cedula or "").strip()
    logger.info(f"[FISCALIA] Consultando cedula: {cedula}")

    # Browser API tiene mejor tasa de exito y menos overhead -> menos intentos
    usando_browser_api = bool(_BROWSER_API_URL)
    if usando_browser_api:
        max_intentos = 2
        logger.info(f"[FISCALIA] Modo: Browser API (Bright Data Scraping Browser)")
    else:
        proxy_cfg = _build_proxy_cfg()
        max_intentos = _MAX_INTENTOS
        if proxy_cfg:
            _p = _obtener_pool()
            if _p:
                logger.info(f"[FISCALIA] Modo: Pool de {len(_p)} proxies residenciales (rotacion automatica)")
            else:
                logger.info(f"[FISCALIA] Modo: Proxy residencial unico -> {proxy_cfg['server']}")
        else:
            logger.info(f"[FISCALIA] Modo: directo (sin proxy)")

    ultimo_error = "sin intentos"
    # Si hay pool, podemos hacer hasta N intentos = tamano del pool (cada uno IP distinta).
    # Si no hay pool, usamos max_intentos default (mismo IP en cada intento, menos util).
    if not usando_browser_api:
        _p = _obtener_pool()
        if _p:
            # Con pool de 1 (rotating proxy → IP nueva cada request) usar MAX_INTENTOS normal (3)
            # Con pool de varios (sticky → IPs distintas por puerto) usar tope 5
            if len(_p) == 1:
                max_intentos = _MAX_INTENTOS  # 3 reintentos contra el mismo endpoint rotativo
            else:
                max_intentos = min(len(_p), 5)
    proxies_usados: list[str] = []  # tracker de servers para excluir en reintentos

    for intento in range(1, max_intentos + 1):
        try:
            if usando_browser_api:
                logger.info(f"[FISCALIA] Intento {intento}/{max_intentos}")
                result = await _intentar_browser_api(cedula)
            else:
                proxy_cfg = _build_proxy_cfg(excluir=proxies_usados)
                if proxy_cfg:
                    proxies_usados.append(proxy_cfg["server"])
                    logger.info(f"[FISCALIA] Intento {intento}/{max_intentos} via {proxy_cfg['server']}")
                else:
                    logger.info(f"[FISCALIA] Intento {intento}/{max_intentos} (sin proxy)")
                result = await _intentar_proxy(cedula, proxy_cfg)
        except Exception as exc:
            logger.error(f"[FISCALIA] Intento {intento} excepcion: {exc}")
            result = _vacio(error=str(exc)[:200])

        if result.get("error") is None or result.get("total_noticias", 0) > 0:
            if intento > 1:
                logger.info(f"[FISCALIA] Exito en intento {intento}")
            return result

        ultimo_error = result.get("error", "desconocido")
        logger.warning(f"[FISCALIA] Intento {intento} fallo: {ultimo_error}")
        if intento < max_intentos:
            await asyncio.sleep(3)

    return _vacio(error=f"Tras {max_intentos} intentos: {ultimo_error}")


async def _intentar_browser_api(cedula: str) -> dict:
    """
    Conecta a Bright Data Browser API (Scraping Browser).
    El browser remoto tiene anti-bot integrado: bypassa Incapsula
    automaticamente, resuelve CAPTCHAs si los hay, rota IPs internamente.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(_BROWSER_API_URL)
        try:
            # Browser API ya viene con contexto residencial, solo creamos page
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="es-EC",
                ignore_https_errors=True,
            )
            page = await ctx.new_page()
            return await _consultar_en_page_browser_api(cedula, page)
        finally:
            await browser.close()


async def _intentar_proxy(cedula: str, proxy_cfg: dict | None) -> dict:
    """
    Toma un browser del pool tibio (ahorra 5-10s de launch), crea un
    BrowserContext aislado y ejecuta la consulta. Devuelve el browser
    al pool al final (el pool rota IPs automáticamente cada N usos).
    """
    pool = get_fiscalia_pool()
    browser = await pool.acquire(timeout=60.0)
    ctx = None
    try:
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="es-EC",
            timezone_id="America/Guayaquil",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "es-EC,es;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        await ctx.add_init_script(_STEALTH_JS)
        page = await ctx.new_page()

        if _HAS_STEALTH_LIB:
            try:
                await stealth_async(page)
            except Exception as e:
                logger.warning(f"[FISCALIA] stealth_async fallo (no critico): {e}")

        return await _consultar_en_page(cedula, page)
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        await pool.release(browser)


async def _consultar_en_page_browser_api(cedula: str, page) -> dict:
    """
    Flujo simplificado para Browser API: el unlock JS lo maneja Bright Data
    internamente, asi que confiamos en que la pagina llegue lista.
    Solo esperamos el form y operamos.
    """
    await page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT_NAV)

    try:
        await page.wait_for_selector("#pwd", state="visible", timeout=_TIMEOUT_PWD_BROWSER_API)
    except PWTimeout:
        return _vacio(error="Browser API: #pwd no aparecio en 45s (sitio caido o cambio de DOM)")

    await page.fill("#pwd", cedula)
    await asyncio.sleep(0.5)
    await page.click("#btn_buscar_denuncia")
    await asyncio.sleep(_SLEEP_CLICK)

    return await _parsear_resultados(cedula, page)


async def _consultar_en_page(cedula: str, page) -> dict:
    """
    Flujo: navegar + esperar challenge JS de Incapsula + reload si hace falta.

    Incapsula sirve un iframe `_Incapsula_Resource` que ejecuta JS para
    verificar el navegador. Si pasa el check, setea cookies y la pagina real
    se renderiza. Damos tiempo al JS + reload para forzar refresh con cookies.
    """
    # Paso 1: goto con wait_until="commit" (solo headers, rapido)
    await page.goto(_URL, wait_until="commit", timeout=_TIMEOUT_NAV)

    # Paso 2: esperar el form #pwd; si aparece rapido, perfecto
    pwd_visible = False
    try:
        await page.wait_for_selector("#pwd", state="visible", timeout=_TIMEOUT_PWD_INICIAL)
        pwd_visible = True
        logger.info("[FISCALIA] #pwd aparecio sin challenge")
    except PWTimeout:
        logger.info("[FISCALIA] #pwd no aparecio, intentando reload post-challenge")

    # Paso 3: si no apareció, puede ser: (a) Incapsula JS challenge en proceso,
    # o (b) hCaptcha esperando resolucion. Damos tiempo JS y verificamos.
    if not pwd_visible:
        await asyncio.sleep(4)   # tiempo para que Incapsula JS termine

        # Antes del reload, ver si la pagina muestra hCaptcha y resolverlo
        try:
            body = await page.content()
            has_hcaptcha = "hcaptcha" in body.lower() or "h-captcha" in body.lower()
            if has_hcaptcha and _TWOCAPTCHA_KEY:
                logger.info("[FISCALIA] Detectado hCaptcha, intentando resolver via 2captcha")
                resolvio = await _detectar_y_resolver_hcaptcha(page)
                if resolvio:
                    # Despues de inyectar el token, esperar a que aparezca #pwd
                    try:
                        await page.wait_for_selector("#pwd", state="visible", timeout=_TIMEOUT_PWD_RELOAD)
                        pwd_visible = True
                        logger.info("[FISCALIA] #pwd aparecio post-hCaptcha resuelto")
                    except PWTimeout:
                        # A veces necesita un submit/click adicional o un reload para aplicar el token
                        try:
                            await page.reload(wait_until="commit", timeout=_TIMEOUT_NAV)
                            await page.wait_for_selector("#pwd", state="visible", timeout=_TIMEOUT_PWD_RELOAD)
                            pwd_visible = True
                            logger.info("[FISCALIA] #pwd aparecio post-hCaptcha+reload")
                        except PWTimeout:
                            pass
        except Exception as e:
            logger.warning(f"[FISCALIA] check hCaptcha fallo: {e}")

        # Si todavia no aparecio, intentar reload tradicional (Incapsula JS challenge)
        if not pwd_visible:
            try:
                await page.reload(wait_until="commit", timeout=_TIMEOUT_NAV)
                await page.wait_for_selector("#pwd", state="visible", timeout=_TIMEOUT_PWD_RELOAD)
                pwd_visible = True
                logger.info("[FISCALIA] #pwd aparecio post-reload")
            except PWTimeout:
                # Capturar HTML para debug
                try:
                    body = await page.content()
                    has_incap = "incapsula" in body.lower() or "_incap_" in body.lower()
                    has_hcap  = "hcaptcha" in body.lower()
                    msg_detalle = "hCaptcha sin resolver" if has_hcap else "Incapsula challenge"
                    logger.warning(f"[FISCALIA] tras reload, #pwd no aparece. incap={has_incap} hcap={has_hcap}")
                except Exception:
                    msg_detalle = "challenge persistente"
                return _vacio(error=f"{msg_detalle} no se resolvio (bot detection persistente)")

    # Paso 4: llenar y submit
    await page.fill("#pwd", cedula)
    await asyncio.sleep(0.5)
    await page.click("#btn_buscar_denuncia")
    await asyncio.sleep(_SLEEP_CLICK)

    return await _parsear_resultados(cedula, page)


async def _parsear_resultados(cedula: str, page) -> dict:
    html = await page.content()
    lower = html.lower()

    if "no se encontraron" in lower or "no existen registro" in lower:
        return _vacio()

    numeros = re.findall(r"NOTICIA DEL DELITO\s+Nro\.\s*([\w]+)", html)
    if not numeros:
        return _vacio()

    noticias         = []
    como_sospechoso  = 0
    como_denunciante = 0
    delitos          = []
    nombre_persona   = ""

    for num in numeros:
        idx = html.find(num)
        siguiente = html.find("NOTICIA DEL DELITO", idx + len(num))
        bloque = html[idx: siguiente if siguiente > 0 else idx + 8000]

        rol = _buscar_rol_cedula(cedula, bloque)
        if rol is None:
            continue

        # Extraer nombre una sola vez (primera noticia donde aparece la cédula)
        if not nombre_persona:
            nombre_persona = _buscar_nombre_cedula(cedula, bloque)

        noticia = _parsear_bloque(num, bloque)
        noticia["rol"] = rol
        noticias.append(noticia)

        if rol in _ROLES_SOSPECHOSO:
            como_sospechoso += 1
            d = noticia.get("delito", "").strip()
            if d and d not in delitos:
                delitos.append(d)
        elif rol in _ROLES_NEUTROS:
            como_denunciante += 1
        # Si rol == "DESCONOCIDO", no contamos en ninguna categoría
        # → cae en OBSERVACIÓN en el semáforo (revisión manual)

    return {
        "error":              None,
        "nombre":             nombre_persona,
        "tiene_antecedentes": como_sospechoso > 0,
        "noticias":           noticias,
        "como_sospechoso":    como_sospechoso,
        "como_denunciante":   como_denunciante,
        "total_noticias":     len(noticias),
        "delitos":            delitos,
    }


def _parsear_bloque(numero: str, bloque: str) -> dict:
    texto = re.sub(r"<[^>]+>", " ", bloque)
    texto = re.sub(r"\s+", " ", texto).strip()

    fecha_m = re.search(r"(\d{4}-\d{2}-\d{2})", texto)
    fecha = fecha_m.group(1) if fecha_m else ""

    delito_m = re.search(r"([A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Z\xc1\xc9\xcd\xd3\xda\xd1\s]+)\s*\(\d+\)", texto)
    if delito_m:
        delito = re.sub(r"\s*\(\d+\)", "", delito_m.group(0)).strip()
    else:
        delito = ""

    # Lugar: "PROVINCIA - CIUDAD" — evitar capturar labels HTML como "LUGAR" o "FECHA"
    lugar_m = re.search(r"\b([A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Z\xc1\xc9\xcd\xd3\xda\xd1\s]{2,20})\s+-\s+([A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Z\xc1\xc9\xcd\xd3\xda\xd1\s]{2,20})\b", texto)
    if lugar_m:
        parte1 = lugar_m.group(1).strip().rstrip()
        parte2 = lugar_m.group(2).strip()
        # Limpiar palabras-label comunes que pueden colarse
        for label in ("LUGAR", "FECHA", "HORA", "ESTADO", "DELITO", "UNIDAD"):
            parte1 = parte1.replace(label, "").strip()
            parte2 = parte2.replace(label, "").strip()
        lugar = f"{parte1} - {parte2}".strip(" -")
    else:
        lugar = ""

    return {"numero": numero, "fecha": fecha, "delito": delito, "lugar": lugar, "rol": ""}


def _buscar_nombre_cedula(cedula: str, bloque_html: str) -> str:
    """
    Extrae el nombre de la persona cuya cédula aparece en la tabla de sujetos.

    Estrategia 1 — columnas <td>:
      La fila <tr> contiene celdas [cédula, nombre, rol, ...].
      Si la cédula está en una celda, el nombre está en la siguiente.

    Estrategia 2 — texto plano:
      Tras la cédula y antes del primer rol conocido, los tokens son el nombre.
    """
    tr_pattern = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    td_pattern = re.compile(r'<td\b[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
    cedula_re = re.compile(rf'(?<!\d){re.escape(cedula)}(?!\d)')
    todos_roles = _ROLES_SOSPECHOSO | _ROLES_NEUTROS

    for tr_match in tr_pattern.finditer(bloque_html):
        tr_content = tr_match.group(1)
        tr_text = re.sub(r"<[^>]+>", " ", tr_content)
        tr_text = re.sub(r"\s+", " ", tr_text).strip()
        if not cedula_re.search(tr_text):
            continue

        # Estrategia 1: celda adyacente a la que contiene la cédula
        tds = td_pattern.findall(tr_content)
        for i, td in enumerate(tds):
            td_text = re.sub(r"<[^>]+>", " ", td)
            td_text = re.sub(r"\s+", " ", td_text).strip()
            if cedula_re.search(td_text):
                if i + 1 < len(tds):
                    nombre_raw = re.sub(r"<[^>]+>", " ", tds[i + 1])
                    nombre_raw = re.sub(r"\s+", " ", nombre_raw).strip()
                    if nombre_raw and nombre_raw.upper() not in todos_roles and len(nombre_raw) > 3:
                        return nombre_raw
                break

        # Estrategia 2: tokens entre cédula y el primer rol en texto plano
        partes = cedula_re.split(tr_text, maxsplit=1)
        if len(partes) > 1:
            tokens = partes[1].strip().split()
            nombre_tokens = []
            for tok in tokens:
                if tok.upper() in todos_roles:
                    break
                nombre_tokens.append(tok)
            nombre = " ".join(nombre_tokens).strip(" ,;-")
            if len(nombre) > 3:
                return nombre

        break  # Solo la primera fila con la cédula

    return ""


def _buscar_rol_cedula(cedula: str, bloque_html: str) -> str | None:
    """
    Busca el rol del sujeto cuya cédula coincide, parseando filas <tr>
    de la tabla SUJETOS exactamente. NO usa fragmento de N chars (que
    contaminaba con filas vecinas tipo "SOSPECHOSOS POR IDENTIFICAR").

    Devuelve:
      - SOSPECHOSO/IMPUTADO/PROCESADO/... si se encontró un rol acusatorio
      - DENUNCIANTE/VICTIMA/PERJUDICADO/... si se encontró un rol neutro
      - "DESCONOCIDO" si la cédula está en una fila pero no se identifica el rol
      - None si la cédula no aparece en ninguna fila de esta noticia
    """
    # Buscar todas las filas <tr> de la tabla
    tr_pattern = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    cedula_escape = re.escape(cedula)
    # Boundary: la cédula debe ser palabra completa (no substring de otra)
    cedula_re = re.compile(rf'(?<!\d){cedula_escape}(?!\d)')

    encontrado_alguna_fila = False
    rol_mas_grave = None   # SOSPECHOSO > NEUTRO > DESCONOCIDO

    for tr_match in tr_pattern.finditer(bloque_html):
        tr_content = tr_match.group(1)
        # Texto limpio de la fila (sin tags, whitespace normalizado)
        tr_text = re.sub(r"<[^>]+>", " ", tr_content)
        tr_text = re.sub(r"\s+", " ", tr_text).strip()

        # ¿Aparece la cédula como palabra exacta en esta fila?
        if not cedula_re.search(tr_text):
            continue

        encontrado_alguna_fila = True
        tr_upper = tr_text.upper()

        # Priorizar SOSPECHOSO (palabra entera con boundaries)
        for rol in _ROLES_SOSPECHOSO:
            if re.search(rf'\b{re.escape(rol)}\b', tr_upper):
                # Si encontramos sospechoso → ya gana (es el peor caso)
                return rol

        # Si no es sospechoso, registrar primer rol neutro encontrado
        if rol_mas_grave is None:
            for rol in _ROLES_NEUTROS:
                if re.search(rf'\b{re.escape(rol)}\b', tr_upper):
                    rol_mas_grave = rol
                    break

    if rol_mas_grave is not None:
        return rol_mas_grave

    if encontrado_alguna_fila:
        # Cédula está en una fila pero no se reconoce el rol
        # NUNCA default a SOSPECHOSO (era el bug viejo) - usar DESCONOCIDO
        logger.warning(f"[FISCALIA] Rol DESCONOCIDO para cedula {cedula}")
        return "DESCONOCIDO"

    return None


def _vacio(error: str | None = None) -> dict:
    return {
        "error":              error,
        "nombre":             "",
        "tiene_antecedentes": False,
        "noticias":           [],
        "como_sospechoso":    0,
        "como_denunciante":   0,
        "total_noticias":     0,
        "delitos":            [],
    }
