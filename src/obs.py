"""
Observabilidad: logging estructurado + integración Sentry opt-in.

Uso:
    from src.obs import capture_exception, init_sentry

    init_sentry(servicio="verifica")   # en main.py al arranque

    try:
        ...
    except Exception as e:
        capture_exception("buscar_individual", e, extra={"cedula": cedula})
        ...

Comportamiento:
- SIEMPRE: imprime nombre del error + str(e) + stack trace completo.
- SI hay SENTRY_DSN en env: también envía a Sentry con extra context.
- SI NO hay SENTRY_DSN: solo stdout (no rompe nada, no requiere registro).

Var de entorno:
    SENTRY_DSN     — DSN del proyecto en sentry.io (opt-in)
    SENTRY_ENV     — environment tag (production / staging / dev)
    SERVICE_NAME   — nombre del servicio para el tag
"""
from __future__ import annotations

import os
import traceback
from typing import Any

# Importar Sentry de forma defensiva — si no está instalado, no rompe
try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    _SENTRY_AVAILABLE = True
except ImportError:
    _SENTRY_AVAILABLE = False

_SENTRY_ACTIVE = False


def init_sentry(servicio: str = "unknown") -> None:
    """
    Inicializa Sentry si SENTRY_DSN está en env. Idempotente.
    Llamar UNA VEZ al arranque de la app (en main.py).
    """
    global _SENTRY_ACTIVE

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        print(f"[obs] Sentry desactivado (sin SENTRY_DSN) — solo logging local")
        return

    if not _SENTRY_AVAILABLE:
        print(f"[obs] ⚠️  SENTRY_DSN configurado pero sentry-sdk no instalado")
        return

    if _SENTRY_ACTIVE:
        return

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENV", "production"),
            release=os.getenv("APP_VERSION", servicio),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_RATE", "0.1")),
            integrations=[FastApiIntegration()],
            send_default_pii=False,        # NO mandar IPs ni headers sensibles
            attach_stacktrace=True,
            max_breadcrumbs=50,
        )
        sentry_sdk.set_tag("service", servicio)
        _SENTRY_ACTIVE = True
        print(f"[obs] ✅ Sentry inicializado para servicio={servicio}")
    except Exception as e:
        print(f"[obs] ❌ Error inicializando Sentry: {e}")


def capture_exception(
    contexto: str,
    exc: BaseException,
    extra: dict[str, Any] | None = None,
    level: str = "error",
) -> None:
    """
    Captura un error: lo imprime con stack trace completo y lo envía a Sentry
    si está activo.

    contexto: descripción corta (ej: "buscar_individual", "pdf_genera")
    exc:      la excepción capturada
    extra:    dict opcional con metadata (cedula, file_id, etc.)
    level:    error|warning|info
    """
    extra = extra or {}
    extra_str = " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""

    # Siempre stdout con traceback
    tb = traceback.format_exc()
    print(f"[{contexto}] ❌ {type(exc).__name__}: {exc}  {extra_str}\n{tb}")

    # Sentry si está activo
    if _SENTRY_ACTIVE and _SENTRY_AVAILABLE:
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("contexto", contexto)
                scope.set_level(level)
                for k, v in extra.items():
                    scope.set_extra(k, v)
                sentry_sdk.capture_exception(exc)
        except Exception as e:
            print(f"[obs] ⚠️  Sentry.capture_exception falló: {e}")


def capture_message(
    mensaje: str,
    extra: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Envía un mensaje informativo (sin exception). Útil para eventos clave."""
    extra = extra or {}
    extra_str = " ".join(f"{k}={v}" for k, v in extra.items())
    print(f"[obs:{level}] {mensaje}  {extra_str}")

    if _SENTRY_ACTIVE and _SENTRY_AVAILABLE:
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_level(level)
                for k, v in extra.items():
                    scope.set_extra(k, v)
                sentry_sdk.capture_message(mensaje)
        except Exception:
            pass
