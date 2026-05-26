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

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from playwright_stealth import stealth_async
    _HAS_STEALTH_LIB = True
except ImportError:
    _HAS_STEALTH_LIB = False

logger = logging.getLogger(__name__)

_URL         = "https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php"
_TIMEOUT_NAV    = 75_000   # navegacion principal (proxy residencial es lento)
_TIMEOUT_WARM   = 12_000   # warming homepage (no critico, falla silencioso)
_TIMEOUT_RES    = 18_000
_SLEEP_INIT     = 3.0
_SLEEP_CLICK    = 4.0

# Proxy residencial (recomendado: Ecuador / LATAM) — opcional
# Formato URL: http://user:pass@host:port  o  socks5://user:pass@host:port
_PROXY_URL  = os.getenv("FISCALIA_PROXY_URL", "").strip()
_PROXY_USER = os.getenv("FISCALIA_PROXY_USER", "").strip()
_PROXY_PASS = os.getenv("FISCALIA_PROXY_PASS", "").strip()


def _build_proxy_cfg() -> dict | None:
    """Construye el dict de proxy para Playwright si las env vars estan seteadas."""
    if not _PROXY_URL:
        return None
    cfg: dict[str, str] = {"server": _PROXY_URL}
    if _PROXY_USER:
        cfg["username"] = _PROXY_USER
    if _PROXY_PASS:
        cfg["password"] = _PROXY_PASS
    return cfg

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

_ROLES_SOSPECHOSO = {"SOSPECHOSO", "IMPUTADO", "PROCESADO", "ACUSADO", "SENTENCIADO", "INVESTIGADO"}
_ROLES_NEUTROS    = {"DENUNCIANTE", "VICTIMA", "OFENDIDO", "TESTIGO", "AGRAVIADO"}


async def consultar_fiscalia(cedula: str) -> dict[str, Any]:
    """
    Consulta la cedula en el SIAF de la Fiscalia General del Estado.

    Returns:
        {
            "error":              None | str,
            "tiene_antecedentes": bool,
            "noticias":           list[dict],
            "como_sospechoso":    int,
            "como_denunciante":   int,
            "total_noticias":     int,
            "delitos":            list[str],
        }
    """
    cedula = (cedula or "").strip()
    logger.info(f"[FISCALIA] Consultando cedula: {cedula}")

    proxy_cfg = _build_proxy_cfg()
    if proxy_cfg:
        logger.info(f"[FISCALIA] Usando proxy residencial: {proxy_cfg['server']}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--lang=es-EC",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="es-EC",
            timezone_id="America/Guayaquil",
            ignore_https_errors=True,   # necesario para proxies con SSL interception (Bright Data)
            extra_http_headers={
                "Accept-Language": "es-EC,es;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        await ctx.add_init_script(_STEALTH_JS)
        page = await ctx.new_page()

        # Aplicar playwright-stealth (cubre TLS, audio, canvas, fonts, etc)
        # Mucho mas completo que nuestro _STEALTH_JS manual
        if _HAS_STEALTH_LIB:
            try:
                await stealth_async(page)
                logger.info("[FISCALIA] playwright-stealth aplicado")
            except Exception as e:
                logger.warning(f"[FISCALIA] stealth_async fallo (no critico): {e}")

        try:
            result = await _consultar_en_page(cedula, page)
        except Exception as exc:
            logger.error(f"[FISCALIA] Error: {exc}")
            result = _vacio(error=str(exc)[:200])
        finally:
            await browser.close()

    return result


async def _consultar_en_page(cedula: str, page) -> dict:
    """
    Flujo: navegar + esperar challenge JS de Incapsula + reload si hace falta.

    Incapsula sirve un iframe `_Incapsula_Resource` que ejecuta JS para
    verificar el navegador. Si pasa el check, setea cookies y la pagina real
    se renderiza. Damos tiempo al JS + reload para forzar refresh con cookies.
    """
    # Paso 1: goto con wait_until="commit" (solo headers, rapido)
    await page.goto(_URL, wait_until="commit", timeout=_TIMEOUT_NAV)

    # Paso 2: esperar el form #pwd hasta 20s; si aparece, perfecto
    pwd_visible = False
    try:
        await page.wait_for_selector("#pwd", state="visible", timeout=20_000)
        pwd_visible = True
        logger.info("[FISCALIA] #pwd aparecio en la primera carga (sin challenge)")
    except PWTimeout:
        logger.info("[FISCALIA] #pwd no aparecio en 20s, asumiendo challenge Incapsula")

    # Paso 3: si no apareció, probablemente estamos en el iframe de Incapsula
    # Damos tiempo extra al JS del challenge para completar, luego reload
    if not pwd_visible:
        await asyncio.sleep(6)   # tiempo para que Incapsula JS termine
        try:
            await page.reload(wait_until="commit", timeout=_TIMEOUT_NAV)
            await page.wait_for_selector("#pwd", state="visible", timeout=30_000)
            pwd_visible = True
            logger.info("[FISCALIA] #pwd aparecio despues del reload post-challenge")
        except PWTimeout:
            # Capturar HTML para debug
            try:
                body = await page.content()
                has_incap = "incapsula" in body.lower() or "_incap_" in body.lower()
                logger.warning(f"[FISCALIA] tras reload, #pwd no aparece. incapsula_en_body={has_incap}")
            except Exception:
                pass
            return _vacio(error="Incapsula challenge no se resolvio (bot detection persistente)")

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

    noticias        = []
    como_sospechoso = 0
    como_denunciante = 0
    delitos         = []

    for num in numeros:
        idx = html.find(num)
        siguiente = html.find("NOTICIA DEL DELITO", idx + len(num))
        bloque = html[idx: siguiente if siguiente > 0 else idx + 8000]

        rol = _buscar_rol_cedula(cedula, bloque)
        if rol is None:
            continue

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

    return {
        "error":              None,
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


def _buscar_rol_cedula(cedula: str, bloque_html: str) -> str | None:
    idx = bloque_html.find(cedula)
    if idx < 0:
        return None

    fragmento = bloque_html[idx: idx + 400]
    fragmento_txt = re.sub(r"<[^>]+>", " ", fragmento).upper()

    for rol in list(_ROLES_SOSPECHOSO) + list(_ROLES_NEUTROS):
        if rol in fragmento_txt:
            return rol

    return "SOSPECHOSO"


def _vacio(error: str | None = None) -> dict:
    return {
        "error":              error,
        "tiene_antecedentes": False,
        "noticias":           [],
        "como_sospechoso":    0,
        "como_denunciante":   0,
        "total_noticias":     0,
        "delitos":            [],
    }
