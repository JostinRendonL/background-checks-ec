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
import re
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

_URL         = "https://www.gestiondefiscalias.gob.ec/siaf/informacion/web/noticiasdelito/index.php"
_TIMEOUT_NAV = 35_000
_TIMEOUT_RES = 18_000
_SLEEP_INIT  = 3.5
_SLEEP_CLICK = 4.0

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver',          {get: () => undefined});
    Object.defineProperty(navigator, 'plugins',            {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages',          {get: () => ['es-EC','es','en-US','en']});
    Object.defineProperty(navigator, 'platform',           {get: () => 'Win32'});
    Object.defineProperty(navigator, 'hardwareConcurrency',{get: () => 8});
    window.chrome = {runtime:{}, loadTimes:function(){}, csi:function(){}, app:{}};
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
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
            extra_http_headers={
                "Accept-Language": "es-EC,es;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        await ctx.add_init_script(_STEALTH_JS)
        page = await ctx.new_page()
        try:
            result = await _consultar_en_page(cedula, page)
        except Exception as exc:
            logger.error(f"[FISCALIA] Error: {exc}")
            result = _vacio(error=str(exc)[:200])
        finally:
            await browser.close()

    return result


async def _consultar_en_page(cedula: str, page) -> dict:
    await page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT_NAV)
    await asyncio.sleep(_SLEEP_INIT)

    if await page.locator("#pwd").count() == 0:
        return _vacio(error="Formulario no accesible (Incapsula bloqueó la IP)")

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
