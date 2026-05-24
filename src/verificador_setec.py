"""
verificador_setec.py — Consulta certificaciones SETEC (Ministerio de Trabajo)

Portal: https://portal.trabajo.gob.ec/setec-portal-web/pages/personasCapacitadasOperadores.jsf
Tecnología: JSF con AJAX parcial (PrimeFaces). Sin CAPTCHA.

Devuelve cursos y horas de formación oficial registradas en el SETEC.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

_URL = (
    "https://portal.trabajo.gob.ec"
    "/setec-portal-web/pages/personasCapacitadasOperadores.jsf"
)
_TIMEOUT    = 30_000   # ms para goto
_AJAX_WAIT  = 15_000   # ms para esperar tabla post-click
_SLEEP_ANTI = 2.0      # segundos anti-ban antes de interactuar

_FRASES_VACIO = [
    "no se encontraron", "no existen registros", "sin registros",
    "no hay datos", "no records found", "0 registros",
    "no se encontró", "lista vacía",
]

# Encabezados de tabla a ignorar al extraer filas
_HEADERS_IGNORAR = {
    "nombre curso / perfil", "nombre curso", "curso", "perfil",
    "número horas", "num horas", "horas", "fecha inicio", "fecha fin",
    "institución", "estado", "acción", "",
}


# ── Función pública (autónoma) ────────────────────────────────────────────────

async def consultar_setec(cedula: str) -> dict[str, Any]:
    """
    Consulta certificaciones SETEC para una cédula.

    Returns:
        {
            "error":              None | str,
            "tiene_certificados": bool,
            "detalle_cursos":     str,   # "CURSO A (60h) | CURSO B (40h)" o "Sin registros"
            "total_cursos":       int,
        }
    """
    cedula = (cedula or "").strip()
    logger.info(f"[SETEC] Consultando cédula: {cedula}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        try:
            resultado = await _consultar_en_page(cedula, page)
        finally:
            await browser.close()

    return resultado


# ── Función con page externo (para reutilizar browser) ────────────────────────

async def check_min_trabajo(cedula: str, page: Page) -> dict[str, Any]:
    """
    Versión que recibe un page ya abierto.
    Útil si el caller maneja el ciclo de vida del browser.
    """
    return await _consultar_en_page(cedula, page)


# ── Lógica principal ──────────────────────────────────────────────────────────

async def _consultar_en_page(cedula: str, page: Page) -> dict[str, Any]:
    try:
        await page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT)
        await asyncio.sleep(_SLEEP_ANTI)

        # 1. Asegurar que el dropdown esté en CÉDULA
        await _seleccionar_cedula(page)

        # 2. Llenar el campo de documento
        await _llenar_cedula(page, cedula)

        # 3. Clic en Buscar
        await _clic_buscar(page)

        # 4. Esperar respuesta AJAX
        await _esperar_ajax(page)

        # 5. Comprobar si la página indica "sin resultados"
        body = (await page.locator("body").inner_text()).lower()
        if any(f in body for f in _FRASES_VACIO):
            logger.info(f"[SETEC] {cedula} → sin registros")
            return _sin_registros()

        # 6. Extraer tabla
        cursos = await _extraer_cursos(page)

        if cursos:
            detalle = " | ".join(cursos)
            logger.info(f"[SETEC] {cedula} → {len(cursos)} cursos")
            return {
                "error":              None,
                "tiene_certificados": True,
                "detalle_cursos":     detalle,
                "total_cursos":       len(cursos),
            }
        else:
            logger.info(f"[SETEC] {cedula} → tabla vacía")
            return _sin_registros()

    except PWTimeout as e:
        logger.warning(f"[SETEC] Timeout para {cedula}: {e}")
        return _error("Portal MDT: timeout de conexión")
    except Exception as e:
        logger.error(f"[SETEC] Error inesperado para {cedula}: {type(e).__name__}: {e}")
        return _error(f"Portal MDT inactivo: {type(e).__name__}")


# ── Helpers de interacción ────────────────────────────────────────────────────

async def _seleccionar_cedula(page: Page) -> None:
    """Selecciona 'CÉDULA' en el dropdown de tipo de filtro."""
    selectores = [
        "select[id*='filtro']",
        "select[id*='tipo']",
        "select[name*='filtro']",
        "select",           # fallback: primer select de la página
    ]
    for sel in selectores:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            try:
                # Intentar por label primero, luego por value
                for opcion in ("CÉDULA", "Cédula", "CEDULA", "cedula", "1"):
                    try:
                        await loc.select_option(label=opcion, timeout=2_000)
                        return
                    except Exception:
                        pass
                await loc.select_option(value="CEDULA", timeout=2_000)
                return
            except Exception:
                pass  # Ya estaba seleccionado o no aplica


async def _llenar_cedula(page: Page, cedula: str) -> None:
    """Llena el campo de documento con la cédula."""
    selectores = [
        "input[id*='documento']",
        "input[id*='Documento']",
        "input[id*='cedula']",
        "input[id*='numDoc']",
        "input[placeholder*='ocumento']",   # "Documento" o "documento"
        "input[placeholder*='édula']",       # "Cédula" o "cédula"
    ]
    for sel in selectores:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            await loc.clear()
            await loc.fill(cedula)
            return
    # Fallback: segundo input de texto (el primero suele ser el del dropdown)
    fallback = page.locator("input[type='text']").nth(1)
    if await fallback.count() > 0:
        await fallback.clear()
        await fallback.fill(cedula)
        return
    raise RuntimeError("No se encontró el campo de cédula en el portal SETEC")


async def _clic_buscar(page: Page) -> None:
    """Hace clic en el botón Buscar."""
    selectores = [
        "button:has-text('Buscar')",
        "input[value='Buscar']",
        "a:has-text('Buscar')",
        "span:has-text('Buscar')",
        "[id*='buscar']",
        "[id*='Buscar']",
    ]
    for sel in selectores:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            await loc.click()
            return
    raise RuntimeError("No se encontró el botón Buscar en el portal SETEC")


async def _esperar_ajax(page: Page) -> None:
    """Espera que el AJAX de JSF termine de actualizar la tabla."""
    # Estrategia 1: networkidle (más confiable en JSF)
    try:
        await page.wait_for_load_state("networkidle", timeout=_AJAX_WAIT)
        return
    except PWTimeout:
        pass
    # Estrategia 2: esperar que aparezca la tabla o un mensaje de vacío
    try:
        await page.wait_for_selector(
            "table tbody tr, [id*='noData'], [class*='ui-datatable-empty']",
            timeout=_AJAX_WAIT,
        )
    except PWTimeout:
        pass  # Si sigue sin tabla, lo detectamos en _extraer_cursos


async def _extraer_cursos(page: Page) -> list[str]:
    """
    Extrae los cursos de la tabla de resultados.
    Columnas esperadas: Nombre Curso / Perfil | ... | Número Horas | ...
    """
    # Buscar la tabla que contenga datos de cursos
    tabla = page.locator("table").filter(has_text="Nombre Curso")
    if await tabla.count() == 0:
        # Fallback: primera tabla con tbody y filas
        tabla = page.locator("table:has(tbody tr)").first

    if await tabla.count() == 0:
        return []

    # Detectar índices de columnas desde el encabezado
    idx_nombre = 0
    idx_horas  = None
    encabezados_raw = await tabla.locator("thead th, thead td").all_inner_texts()
    encabezados = [h.strip().lower() for h in encabezados_raw]
    for i, h in enumerate(encabezados):
        if "nombre" in h or "curso" in h or "perfil" in h:
            idx_nombre = i
        if "hora" in h:
            idx_horas = i

    # Extraer filas del tbody
    filas = tabla.locator("tbody tr")
    n = await filas.count()
    cursos: list[str] = []

    for i in range(n):
        celdas_raw = await filas.nth(i).locator("td").all_inner_texts()
        celdas = [c.strip() for c in celdas_raw]

        if not celdas:
            continue

        nombre = celdas[idx_nombre] if idx_nombre < len(celdas) else ""
        if not nombre or nombre.lower() in _HEADERS_IGNORAR:
            continue

        horas = ""
        if idx_horas is not None and idx_horas < len(celdas):
            h = celdas[idx_horas].strip()
            if h.isdigit():
                horas = h
        else:
            # Buscar horas heurísticamente: número razonable (1-999)
            for c in celdas[1:]:
                limpio = c.strip()
                if limpio.isdigit() and 1 <= int(limpio) <= 999:
                    horas = limpio
                    break

        entrada = f"{nombre.upper()} ({horas}h)" if horas else nombre.upper()
        cursos.append(entrada)

    return cursos


# ── Helpers de resultado ──────────────────────────────────────────────────────

def _sin_registros() -> dict[str, Any]:
    return {
        "error":              None,
        "tiene_certificados": False,
        "detalle_cursos":     "Sin registros",
        "total_cursos":       0,
    }


def _error(msg: str) -> dict[str, Any]:
    return {
        "error":              msg,
        "tiene_certificados": False,
        "detalle_cursos":     "N/A",
        "total_cursos":       0,
    }
