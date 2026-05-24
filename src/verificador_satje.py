"""
verificador_satje.py — Módulo 3: Consulta SATJE (Función Judicial)

Verifica si una persona tiene procesos judiciales en Ecuador consultando
la API pública del sistema E-SATJE 2020.

DESCUBRIMIENTO CLAVE (Mayo 2026):
  - La API REST es completamente pública (sin auth, sin reCAPTCHA real)
  - No hay Incapsula ni WAF en el endpoint de API
  - Funciona con httpx puro desde cualquier IP (VPS, local, etc.)
  - NO requiere Playwright ni browser

API base: https://api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-SERVICE/api/consulta-causas/

Endpoints confirmados:
  POST informacion/buscarCausas?page=0&size=N  → lista de causas
  POST informacion/contarCausas                → total de causas

Body (ambos endpoints):
  {
    "numeroCausa": null,
    "actor":     {"cedulaActor": "<cedula>",  "nombreActor": ""},
    "demandado": {"cedulaDemandado": "<cedula>", "nombreDemandado": ""},
    "provincia": null,
    "numeroFiscalia": null,
    "recaptcha": "x"   ← el servidor NO valida este campo
  }

Uso:
  resultado = asyncio.run(consultar_satje("0912345675"))
  print(resultado)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

_BASE_URL = (
    "https://api.funcionjudicial.gob.ec"
    "/EXPEL-CONSULTA-CAUSAS-SERVICE/api/consulta-causas/"
)
_EP_BUSCAR  = "informacion/buscarCausas"
_EP_CONTAR  = "informacion/contarCausas"

_HEADERS = {
    "Content-Type": "application/json",
    "Origin":       "https://procesosjudiciales.funcionjudicial.gob.ec",
}

_PAGE_SIZE   = 50   # causas por página
_TIMEOUT_SEG = 20


# ── Función principal ─────────────────────────────────────────────────────────

async def consultar_satje(cedula: str) -> dict[str, Any]:
    """
    Consulta si una persona tiene procesos judiciales en SATJE.

    Busca la cédula tanto como ACTOR (demandante/víctima)
    como DEMANDADO (acusado/procesado).

    Returns dict con estructura:
    {
        "cedula":        "0912345675",
        "status":        "SIN_PROCESOS" | "CON_PROCESOS" | "ERROR",
        "total_actor":   0,
        "total_demandado": 0,
        "total":         0,
        "causas_demandado": [...],
        "causas_actor":    [...],
        "detalle":       "descripción del error si aplica"
    }
    """
    cedula = cedula.strip()
    logger.info(f"[SATJE] Consultando cédula: {cedula}")

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=_TIMEOUT_SEG,
            follow_redirects=True,
        ) as client:

            # Buscar como demandado (más relevante para background checks)
            causas_dem, total_dem = await _buscar_por_rol(
                client, cedula, rol="demandado"
            )

            # Buscar como actor (víctima/demandante)
            causas_act, total_act = await _buscar_por_rol(
                client, cedula, rol="actor"
            )

        total = total_dem + total_act

        resultado = {
            "cedula":            cedula,
            "status":            "SIN_PROCESOS" if total == 0 else "CON_PROCESOS",
            "total":             total,
            "total_demandado":   total_dem,
            "total_actor":       total_act,
            "causas_demandado":  causas_dem,
            "causas_actor":      causas_act,
        }

        logger.info(
            f"[SATJE] {cedula} → {resultado['status']} "
            f"(demandado={total_dem}, actor={total_act})"
        )
        return resultado

    except httpx.TimeoutException:
        logger.error(f"[SATJE] Timeout consultando {cedula}")
        return _error(cedula, "Timeout al conectar con API de SATJE")
    except httpx.HTTPError as e:
        logger.error(f"[SATJE] HTTPError {e}")
        return _error(cedula, f"Error HTTP: {e}")
    except Exception as e:
        logger.exception(f"[SATJE] Error inesperado: {e}")
        return _error(cedula, f"Error inesperado: {e}")


# ── Helpers internos ──────────────────────────────────────────────────────────

async def _buscar_por_rol(
    client: httpx.AsyncClient,
    cedula: str,
    rol: str,   # "actor" | "demandado"
) -> tuple[list[dict], int]:
    """
    Consulta causas donde la cédula aparece en el rol indicado.
    Pagina automáticamente hasta obtener todas las causas.
    """
    body_base = _construir_body(cedula, rol)

    # 1. Contar total
    try:
        r = await client.post(_BASE_URL + _EP_CONTAR, json=body_base)
        r.raise_for_status()
        total = int(r.json())
    except Exception as e:
        logger.warning(f"[SATJE] Error contando {rol} para {cedula}: {e}")
        total = 0

    if total == 0:
        return [], 0

    # 2. Obtener todas las causas (paginado)
    causas: list[dict] = []
    paginas = (total + _PAGE_SIZE - 1) // _PAGE_SIZE

    for pagina in range(paginas):
        try:
            r = await client.post(
                f"{_BASE_URL}{_EP_BUSCAR}?page={pagina}&size={_PAGE_SIZE}",
                json=body_base,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                causas.extend([_normalizar_causa(c, rol) for c in data])
        except Exception as e:
            logger.warning(f"[SATJE] Error en página {pagina}: {e}")
            break

    return causas, total


def _construir_body(cedula: str, rol: str) -> dict:
    """Construye el body según el rol de la cédula."""
    if rol == "actor":
        return {
            "numeroCausa":    None,
            "actor":          {"cedulaActor": cedula, "nombreActor": ""},
            "demandado":      {"cedulaDemandado": "", "nombreDemandado": ""},
            "provincia":      None,
            "numeroFiscalia": None,
            "recaptcha":      "x",
        }
    else:  # demandado
        return {
            "numeroCausa":    None,
            "actor":          {"cedulaActor": "", "nombreActor": ""},
            "demandado":      {"cedulaDemandado": cedula, "nombreDemandado": ""},
            "provincia":      None,
            "numeroFiscalia": None,
            "recaptcha":      "x",
        }


def _normalizar_causa(raw: dict, rol: str) -> dict:
    """Normaliza una causa del API a un formato limpio."""
    return {
        "idJuicio":         raw.get("idJuicio", ""),
        "rol":              rol,
        "delito":           raw.get("nombreDelito") or "",
        "materia":          raw.get("nombreMateria") or "",
        "fechaIngreso":     (raw.get("fechaIngreso") or "")[:10],
        "estadoActual":     raw.get("estadoActual") or "",
        "estadoJuicio":     raw.get("nombreEstadoJuicio") or "",
        "judicatura":       raw.get("nombreJudicatura") or "",
        "provincia":        raw.get("nombreProvincia") or "",
        "tipoAccion":       raw.get("nombreTipoAccion") or "",
        # Nombres (útiles como fallback si Bachiller no encuentra)
        "nombreActor":      raw.get("nombreActor") or "",
        "nombreDemandado":  raw.get("nombreDemandado") or "",
    }


def _error(cedula: str, detalle: str) -> dict:
    return {
        "cedula":            cedula,
        "status":            "ERROR",
        "total":             0,
        "total_demandado":   0,
        "total_actor":       0,
        "causas_demandado":  [],
        "causas_actor":      [],
        "detalle":           detalle,
    }
