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
async def health():
    """Healthcheck — confirma que el servidor está operativo."""
    return {
        "status": "ok",
        "version": "1.1.0",
        "modulos": {
            "bachiller": "activo",
            "satje":     "activo",
            "setec":     "activo",
        }
    }


@app.post("/consultar/bachiller", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_bachiller_ep(req: ConsultaRequest):
    """
    Verifica si la persona tiene título de bachiller registrado
    en el Ministerio de Educación.
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /bachiller → {cedula}")
    t0 = time.time()
    async with _semaphore:
        resultado = await asyncio.to_thread(verificar_bachiller, cedula)
    resultado["tiempo_seg"] = round(time.time() - t0, 2)
    return resultado


@app.post("/consultar/satje", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_satje_ep(req: ConsultaRequest):
    """
    Verifica procesos judiciales en el sistema SATJE de la Función Judicial.
    Busca la cédula como demandado/procesado y como actor/ofendido.
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /satje → {cedula}")
    t0 = time.time()
    async with _semaphore:
        resultado = await consultar_satje(cedula)
    resultado["tiempo_seg"] = round(time.time() - t0, 2)
    return resultado


@app.post("/consultar/setec", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_setec_ep(req: ConsultaRequest):
    """
    Verifica certificaciones de capacitación registradas en el SETEC
    (Ministerio de Trabajo).
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /setec → {cedula}")
    t0 = time.time()
    async with _semaphore:
        resultado = await consultar_setec(cedula)
    resultado["tiempo_seg"] = round(time.time() - t0, 2)
    return resultado


@app.post("/consultar/completo", tags=["Verificaciones"], dependencies=[Depends(verificar_api_key)])
async def consultar_completo_ep(req: ConsultaRequest):
    """
    Ejecuta todos los módulos en paralelo: bachiller, SATJE y SETEC.
    Devuelve un reporte consolidado con semáforo de riesgo.
    """
    cedula = req.cedula.strip()
    _validar_cedula(cedula)
    logger.info(f"[API] /completo → {cedula}")
    t0 = time.time()

    async with _semaphore:
        resultados = await asyncio.gather(
            asyncio.to_thread(verificar_bachiller, cedula),
            consultar_satje(cedula),
            consultar_setec(cedula),
            return_exceptions=True,
        )

    bachiller_res = resultados[0] if not isinstance(resultados[0], Exception) else {"estado": "ERROR", "detalle": str(resultados[0])}
    satje_res     = resultados[1] if not isinstance(resultados[1], Exception) else {"status": "ERROR", "detalle": str(resultados[1])}
    setec_res     = resultados[2] if not isinstance(resultados[2], Exception) else {"error": str(resultados[2]), "tiene_certificados": False, "detalle_cursos": "N/A", "total_cursos": 0}

    semaforo = _calcular_semaforo(bachiller_res, satje_res)

    return {
        "cedula":     cedula,
        "semaforo":   semaforo,
        "bachiller":  bachiller_res,
        "satje":      satje_res,
        "setec":      setec_res,
        "tiempo_seg": round(time.time() - t0, 2),
    }


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
