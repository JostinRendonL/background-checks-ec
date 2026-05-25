"""
Métricas Prometheus opt-in.

Si METRICS_ENABLED=1 en env, expone /metrics con métricas FastAPI automáticas
(latencias por endpoint, contadores, errores, etc.).

Sin la env var no rompe nada — el endpoint /metrics simplemente devuelve 404.

Var de entorno:
    METRICS_ENABLED  — '1' para activar
"""
from __future__ import annotations
import os


def setup_metrics(app) -> None:
    """
    Adjunta /metrics al app si METRICS_ENABLED y prometheus-fastapi-instrumentator
    está instalado. Idempotente.
    """
    if os.getenv("METRICS_ENABLED", "0") != "1":
        return

    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        print("[metrics] METRICS_ENABLED=1 pero prometheus-fastapi-instrumentator "
              "no instalado — instala con: pip install prometheus-fastapi-instrumentator")
        return

    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
        env_var_name="METRICS_ENABLED",
    ).instrument(app).expose(app, endpoint="/metrics", tags=["Sistema"])

    print("[metrics] ✅ /metrics expuesto (Prometheus)")
