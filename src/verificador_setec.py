"""
verificador_setec.py — Consulta certificaciones SETEC (Ministerio de Trabajo)

Portal: https://portal.trabajo.gob.ec/setec-portal-web/pages/personasCapacitadasOperadores.jsf
Tecnología: JSF con AJAX parcial (PrimeFaces). Sin CAPTCHA.

Flujo real del portal (inspeccionado 2026-05-24):
  1. Hay un SelectOneMenu PrimeFaces cuyo <select> oculto tiene ID que contiene
     'cmbSubOcIndependiente_input'. CÉDULA = value "0".
  2. Al cambiar dispara PrimeFaces.ab() → AJAX que muestra el panel con:
     - Input texto: id contiene 'txtParametroCapDoc'
     - Botón Buscar / Cancelar
  3. Click Buscar → AJAX que muestra tabla de resultados.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

_URL        = "https://portal.trabajo.gob.ec/setec-portal-web/pages/personasCapacitadasOperadores.jsf"
_TIMEOUT    = 30_000   # ms para goto
_AJAX_WAIT  = 18_000   # ms para esperar panel / tabla post-AJAX
_SLEEP_INIT = 2.0      # segundos tras goto (anti-ban)

_FRASES_VACIO = [
    "no se encontraron", "no existen registros", "sin registros",
    "no hay datos", "no records found", "0 registros",
    "no se encontró", "lista vacía",
]

_HEADERS_IGNORAR = {
    "nombre curso / perfil", "nombre curso", "curso", "perfil",
    "número horas", "num horas", "horas", "fecha inicio", "fecha fin",
    "institución", "estado", "acción", "",
}


# ── Función pública (autónoma) ────────────────────────────────────────────────

# ── Pool singleton de browsers SETEC ────────────────────────────────────────
# Evita race condition de uvloop al hacer async_playwright().start() en cada
# request (manifestaba "Racing with another loop to spawn a process" con
# 2+ consultas concurrentes).
from src.browser_pool import BrowserPool
import os

_setec_pool: BrowserPool | None = None


def get_setec_pool() -> BrowserPool:
    """Pool singleton de browsers para SETEC (sin proxy, conexion directa)."""
    global _setec_pool
    if _setec_pool is None:
        size = int(os.getenv("SETEC_POOL_SIZE", "3"))
        _setec_pool = BrowserPool(
            size=size,
            launch_kwargs={
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            },
            max_uses_per_browser=50,
            name="setec",
        )
    return _setec_pool


async def consultar_setec(cedula: str) -> dict[str, Any]:
    """
    Consulta certificaciones SETEC para una cédula.

    Returns:
        {
            "error":              None | str,
            "tiene_certificados": bool,
            "detalle_cursos":     str,
            "total_cursos":       int,
        }
    """
    cedula = (cedula or "").strip()
    logger.info(f"[SETEC] Consultando cédula: {cedula}")

    pool = get_setec_pool()
    browser = await pool.acquire(timeout=60.0)
    ctx = None
    try:
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        return await _consultar_en_page(cedula, page)
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        await pool.release(browser)


# ── Función con page externo (para reutilizar browser) ────────────────────────

async def check_min_trabajo(cedula: str, page: Page) -> dict[str, Any]:
    """Versión que recibe un page ya abierto."""
    return await _consultar_en_page(cedula, page)


# ── Lógica principal ──────────────────────────────────────────────────────────

async def _consultar_en_page(cedula: str, page: Page) -> dict[str, Any]:
    try:
        # 1. Cargar portal
        await page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT)
        await asyncio.sleep(_SLEEP_INIT)

        # 2. Seleccionar tipo "CÉDULA" en el SelectOneMenu de PrimeFaces
        await _seleccionar_cedula(page)

        # 3. Esperar que el AJAX muestre el panel con el input de cédula
        await _esperar_panel_cedula(page)

        # 4. Llenar el campo de cédula
        await _llenar_cedula(page, cedula)

        # 5. Clic en Buscar
        await _clic_buscar(page)

        # 6. Esperar respuesta AJAX de la tabla (o del mensaje vacío)
        await _esperar_ajax(page)

        # 7. Comprobar mensaje de "sin resultados" usando elemento específico
        #    de PrimeFaces (tr.ui-datatable-empty-message) — NO el body completo,
        #    porque "sin registros" puede aparecer en tooltips o templates ocultos.
        empty_msg = page.locator(
            "tr.ui-datatable-empty-message, "
            "div.ui-datatable-empty-message, "
            "[class*='datatable-empty']"
        )
        if await empty_msg.count() > 0:
            try:
                if await empty_msg.first.is_visible():
                    logger.info(f"[SETEC] {cedula} → sin registros (empty-message visible)")
                    return _sin_registros()
            except Exception:
                pass

        # 8. Extraer tabla
        cursos, nombre_detectado = await _extraer_cursos_y_nombre(page)

        if cursos:
            detalle = " | ".join(cursos)
            logger.info(f"[SETEC] {cedula} → {len(cursos)} cursos")
            return {
                "error":              None,
                "tiene_certificados": True,
                "detalle_cursos":     detalle,
                "total_cursos":       len(cursos),
                "nombre":             nombre_detectado or "",
            }
        else:
            logger.info(f"[SETEC] {cedula} → tabla vacía")
            return _sin_registros()

    except PWTimeout as e:
        logger.warning(f"[SETEC] Timeout para {cedula}: {e}")
        return _error("Portal MDT: timeout de conexión")
    except Exception as e:
        logger.error(f"[SETEC] Error inesperado para {cedula}: {type(e).__name__}: {e}")
        return _error(f"Portal MDT inactivo: {type(e).__name__}: {str(e)[:80]}")


# ── Helpers de interacción ────────────────────────────────────────────────────

async def _seleccionar_cedula(page: Page) -> None:
    """
    Selecciona 'CÉDULA' (value='0') en el SelectOneMenu PrimeFaces.
    El <select> oculto tiene ID que contiene 'cmbSubOcIndependiente_input'.
    Dispara el onchange de PrimeFaces vía evaluate para que el AJAX se ejecute.
    """
    # Selector exacto descubierto en inspección DOM (2026-05-24)
    selectores = [
        "select[id*='cmbSubOcIndependiente_input']",
        "select[id*='cmbSubOc']",
        "select",   # fallback amplio
    ]
    for sel_css in selectores:
        loc = page.locator(sel_css).first
        if await loc.count() == 0:
            continue
        try:
            # Obtener el ID del select para ejecutar su onchange correctamente
            sel_id = await loc.get_attribute("id")
            # 1. Setear el valor a "0" (CÉDULA)
            await page.evaluate(
                f"""
                (function() {{
                    var sel = document.getElementById('{sel_id}');
                    if (!sel) return;
                    sel.value = '0';
                    var oc = sel.getAttribute('onchange');
                    if (oc) eval(oc);
                }})()
                """
            )
            logger.debug(f"[SETEC] select_cedula OK via {sel_css}")
            return
        except Exception as exc:
            logger.debug(f"[SETEC] _seleccionar_cedula {sel_css} falló: {exc}")
            continue

    # Último intento: select_option de Playwright (puede no disparar PrimeFaces AJAX)
    try:
        await page.locator("select").first.select_option(value="0", timeout=3_000)
    except Exception:
        pass


async def _esperar_panel_cedula(page: Page) -> None:
    """
    Espera a que el panel del formulario (con el input de cédula) sea visible
    después del AJAX disparado por _seleccionar_cedula.
    """
    # El input de cédula tiene ID que contiene 'txtParametroCapDoc'
    try:
        await page.wait_for_selector(
            "input[id*='txtParametroCapDoc']",
            state="visible",
            timeout=_AJAX_WAIT,
        )
        return
    except PWTimeout:
        pass
    # Fallback: networkidle
    try:
        await page.wait_for_load_state("networkidle", timeout=_AJAX_WAIT)
    except PWTimeout:
        pass


async def _llenar_cedula(page: Page, cedula: str) -> None:
    """
    Llena el input de número de documento con la cédula.
    ID descubierto: contiene 'txtParametroCapDoc'.
    """
    selectores = [
        "input[id*='txtParametroCapDoc']",   # ID exacto confirmado
        "input[id*='txtParametro']",
        "input[id*='numDoc']",
        "input[id*='NumDoc']",
        "input[id*='documento']",
        "input[id*='Documento']",
        "input[id*='cedula']",
        "input[id*='Cedula']",
    ]
    for sel_css in selectores:
        loc = page.locator(sel_css).first
        if await loc.count() > 0:
            try:
                if await loc.is_visible():
                    await loc.clear()
                    await loc.fill(cedula)
                    logger.debug(f"[SETEC] _llenar_cedula OK via {sel_css}")
                    return
            except Exception:
                continue

    # Fallback: primer input de texto visible que no sea el focus del combobox
    all_inputs = page.locator("input[type='text']")
    count = await all_inputs.count()
    for i in range(count):
        inp = all_inputs.nth(i)
        inp_id = await inp.get_attribute("id") or ""
        if "focus" in inp_id or "filter" in inp_id:
            continue
        try:
            if await inp.is_visible():
                await inp.clear()
                await inp.fill(cedula)
                logger.debug(f"[SETEC] _llenar_cedula OK fallback input #{i}")
                return
        except Exception:
            continue

    raise RuntimeError("No se encontró el campo de cédula en el portal SETEC")


async def _clic_buscar(page: Page) -> None:
    """
    Hace clic en el botón Buscar.
    Confirmado como <button> con texto 'Buscar'.
    """
    selectores = [
        "button:has-text('Buscar')",
        "input[value='Buscar']",
        "a:has-text('Buscar')",
        "[id*='btnBuscar']",
        "[id*='buscar']:not([id*='cancel']):not([id*='limpiar'])",
    ]
    for sel_css in selectores:
        loc = page.locator(sel_css).first
        if await loc.count() > 0:
            try:
                if await loc.is_visible():
                    await loc.click()
                    logger.debug(f"[SETEC] _clic_buscar OK via {sel_css}")
                    return
            except Exception:
                continue

    raise RuntimeError("No se encontró el botón Buscar en el portal SETEC")


async def _esperar_ajax(page: Page) -> None:
    """Espera que el AJAX de búsqueda actualice la tabla de resultados."""
    try:
        await page.wait_for_load_state("networkidle", timeout=_AJAX_WAIT)
        return
    except PWTimeout:
        pass
    try:
        await page.wait_for_selector(
            "table tbody tr, [id*='noData'], [class*='ui-datatable-empty']",
            timeout=_AJAX_WAIT,
        )
    except PWTimeout:
        pass


async def _extraer_cursos_y_nombre(page: Page) -> tuple[list[str], str | None]:
    """
    Wrapper que devuelve también el nombre de la persona (col 1: "Apellidos / Nombres").
    """
    cursos = await _extraer_cursos(page)
    nombre = await _extraer_nombre_setec(page)
    return cursos, nombre


async def _extraer_nombre_setec(page: Page) -> str | None:
    """
    Extrae el nombre de la persona de la columna "Apellidos / Nombres"
    (índice fijo 1 en la tabla de resultados SETEC).
    """
    try:
        tabla = page.locator("div.ui-datatable-tablewrapper table[role='grid']").first
        if await tabla.count() == 0:
            tabla = page.locator("table[role='grid']").first
        if await tabla.count() == 0:
            return None
        primera = tabla.locator("tbody tr").first
        if await primera.count() == 0:
            return None
        cls = await primera.get_attribute("class") or ""
        if "ui-datatable-empty-message" in cls:
            return None
        celdas = await primera.locator("td").all_inner_texts()
        if len(celdas) > 1:
            nombre = " ".join(celdas[1].split()).strip()
            if nombre and len(nombre) > 3:
                return nombre.upper()
    except Exception as exc:
        logger.debug(f"[SETEC] _extraer_nombre_setec: {exc}")
    return None


async def _extraer_cursos(page: Page) -> list[str]:
    """
    Extrae los cursos de la tabla de resultados.

    Estructura confirmada del DOM (PrimeFaces <p:dataTable> 2026-05-24):
      <div class="ui-datatable-tablewrapper">
        <table role="grid">
          <thead>
            <tr>
              <th>Número Documento</th>        # 0
              <th>Apellidos / Nombres</th>     # 1
              <th>Tipo Capacitación</th>       # 2
              <th>Nombre Curso / Perfil</th>   # 3  ← curso
              <th>Número Horas</th>            # 4  ← horas
              <th>Razón Social OC</th>         # 5
              <th>Nombre Comercial OC</th>     # 6  (puede estar vacía)
              <th>Número Certificado</th>      # 7
            </tr>
          </thead>
          <tbody><tr>...</tr></tbody>
        </table>
      </div>

    BUG HISTÓRICO: el match anterior usaba `if "nombre" in h` y por eso elegía
    la columna 6 ("Nombre Comercial OC") como idx_nombre — esa columna está
    vacía para muchas personas → la fila se descartaba → falso negativo.
    Fix: matchear sólo por "curso" o "perfil" (específicos y unívocos).
    """
    # 1. Elegir la tabla correcta — preferimos role=grid dentro del wrapper.
    tabla = page.locator("div.ui-datatable-tablewrapper table[role='grid']").first
    if await tabla.count() == 0:
        tabla = page.locator("table[role='grid']").first
    if await tabla.count() == 0:
        tabla = page.locator("table:has(tbody tr)").first
    if await tabla.count() == 0:
        return []

    # 2. Detectar columnas — "curso"/"perfil" es específico; "nombre" no lo es
    #    (matchea "Apellidos / Nombres" y "Nombre Comercial OC").
    idx_nombre: int | None = None
    idx_horas:  int | None = None
    encabezados_raw = await tabla.locator("thead th, thead td").all_inner_texts()
    encabezados = [h.strip().lower() for h in encabezados_raw]
    for i, h in enumerate(encabezados):
        if "curso" in h or "perfil" in h:
            idx_nombre = i
        if "hora" in h:
            idx_horas = i

    # Fallback si no se detectó por header: usar posición fija conocida
    if idx_nombre is None:
        idx_nombre = 3   # Nombre Curso / Perfil
    if idx_horas is None:
        idx_horas = 4    # Número Horas

    # 3. Iterar filas tbody y armar la lista
    filas = tabla.locator("tbody tr")
    n = await filas.count()
    cursos: list[str] = []

    for i in range(n):
        # Saltar fila de "empty message" de PrimeFaces
        fila_class = await filas.nth(i).get_attribute("class") or ""
        if "ui-datatable-empty-message" in fila_class:
            continue

        celdas_raw = await filas.nth(i).locator("td").all_inner_texts()
        celdas = [c.strip() for c in celdas_raw]
        if not celdas:
            continue

        nombre = celdas[idx_nombre] if idx_nombre < len(celdas) else ""
        nombre = " ".join(nombre.split())  # colapsar whitespace interno
        if not nombre or nombre.lower() in _HEADERS_IGNORAR:
            continue

        horas = ""
        if idx_horas is not None and idx_horas < len(celdas):
            h = celdas[idx_horas].strip()
            if h.isdigit():
                horas = h

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
        "nombre":             "",
    }


def _error(msg: str) -> dict[str, Any]:
    return {
        "error":              msg,
        "tiene_certificados": False,
        "detalle_cursos":     "N/A",
        "total_cursos":       0,
        "nombre":             "",
    }
