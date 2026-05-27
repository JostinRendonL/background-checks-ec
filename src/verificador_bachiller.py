import os
import base64
import threading
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Concurrencia segura de sync_playwright ─────────────────────────────────
# sync_playwright NO es thread-safe — múltiples threads haciendo
# sync_playwright().start() simultáneamente crean race condition al
# spawnear el subproceso Node.js del driver Playwright. Resultado:
# excepciones intermitentes en lotes grandes -> 88% ERROR en batch de 225.
#
# Fix: semáforo que limita a N consultas Bachiller concurrentes. N=2 por
# default es seguro (2 procesos Chromium tibios cabe en RAM y no race).
# Configurable con BACHILLER_CONCURRENCY env var.
_BACHILLER_SEMAPHORE = threading.Semaphore(
    int(os.getenv("BACHILLER_CONCURRENCY", "2"))
)

MINISTERIO_URL = (
    "https://servicios.educacion.gob.ec"
    "/titulacion25-web/faces/paginas/consulta-titulos-refrendados.xhtml"
)

_FRASE_NO_ENCONTRADO_EXACTA = (
    "no existe registro de título de bachiller"
)

_FRASES_CAPTCHA_MAL = [
    "captcha incorrecto", "código incorrecto", "captcha inválido",
    "código de seguridad", "inválido", "error en el captcha",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Resolución de captcha ─────────────────────────────────────────────────────

_TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_API_KEY", "").strip()


def _resolver_captcha_2captcha(img_b64: str) -> str:
    """
    Resuelve captcha via 2Captcha (servicio humano, sin rate limits).
    Costo: ~$0.001 por captcha. Tiempo: 5-15 seg.
    """
    import time

    # 1. Enviar imagen a la cola
    r = httpx.post(
        "https://2captcha.com/in.php",
        data={
            "key":    _TWOCAPTCHA_KEY,
            "method": "base64",
            "body":   img_b64,
            "json":   "1",
        },
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != 1:
        raise ValueError(f"2Captcha rechazo: {data.get('request')}")
    task_id = data["request"]

    # 2. Esperar resolución (poll cada 5 seg, max 60 seg)
    for _ in range(12):
        time.sleep(5)
        r2 = httpx.get(
            "https://2captcha.com/res.php",
            params={"key": _TWOCAPTCHA_KEY, "action": "get",
                    "id": task_id, "json": "1"},
            timeout=10.0,
        )
        r2.raise_for_status()
        d2 = r2.json()
        if d2.get("status") == 1:
            return d2["request"].strip()
        if d2.get("request") != "CAPCHA_NOT_READY":
            raise ValueError(f"2Captcha error: {d2.get('request')}")

    raise TimeoutError("2Captcha no respondió en 60 segundos")


def _resolver_captcha_openrouter(img_b64: str) -> str:
    """
    Fallback: resuelve captcha via Gemini 2.0 Flash en OpenRouter.
    Puede dar 429 si hay muchas llamadas en poco tiempo.
    """
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://rubasa.com.ec",
            "X-Title":       "RUBASA Bachiller Checker",
        },
        json={
            "model": "google/gemini-2.0-flash-001",
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Esta es una imagen de CAPTCHA del sitio web del Ministerio de Educación "
                            "de Ecuador. El captcha contiene SOLO letras minúsculas (a-z) y dígitos (0-9), "
                            "nunca mayúsculas ni símbolos especiales. "
                            "Presta atención especial a estas confusiones frecuentes: "
                            "'0' (cero) vs 'o' (letra o), "
                            "'1' (uno) vs 'l' (letra ele) vs 'i' (letra i), "
                            "'5' vs 's', '8' vs 'b'. "
                            "Devuelve ÚNICAMENTE los caracteres que ves, sin espacios, "
                            "sin explicación, sin puntuación. Solo los caracteres exactos del captcha."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }],
            "temperature": 0.0,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _resolver_captcha(img_b64: str) -> str:
    """
    Usa 2Captcha si la key está configurada (preferido — sin rate limits).
    Cae a OpenRouter (Gemini) si no hay key de 2Captcha.
    """
    if _TWOCAPTCHA_KEY:
        try:
            return _resolver_captcha_2captcha(img_b64)
        except Exception as e:
            print(f"[captcha] 2Captcha falló ({e}), usando OpenRouter como fallback")
    return _resolver_captcha_openrouter(img_b64)


# ── Selectores con fallback (JSF no garantiza IDs estáticos) ─────────────────

def _campo_cedula(page):
    for sel in [
        "input[id*='cedula']", "input[id*='Cedula']",
        "input[id*='identificacion']", "input[id*='numDoc']",
        "input[id*='numero']", "input[name*='cedula']",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc
    return page.locator("input[type='text']").first


def _campo_captcha(page):
    for sel in [
        "input[id*='captcha']", "input[id*='Captcha']",
        "input[name*='captcha']", "input[id*='codigoSeguridad']",
        "input[id*='codigo']",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc
    return page.locator("input[type='text']").nth(1)


def _boton_buscar(page):
    for sel in [
        "input[id*='clBuscar']",
        "input[id*='buscar']", "input[id*='Buscar']",
        "input[value*='Buscar']", "button:has-text('Buscar')",
        "input[value*='Consultar']", "button:has-text('Consultar')",
        "input[type='submit']", "button[type='submit']",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc
    return page.locator("input[type='submit'], button[type='submit']").first


# ── Extracción de resultados ──────────────────────────────────────────────────

def _extraer_resultado(page, cedula: str) -> dict:
    """
    Interpreta la página actual tras el submit.
    Columnas de la tabla rf-dt:
      Nº(0) | Nº Identifi.(1) | Nombre(2) | Institución(3) |
      Título(4) | Especialidad(5) | Fecha Grado(6) | Nº Refrendación(7)
    """
    texto_pagina = page.inner_text("body").lower()

    # Mensaje oficial del Ministerio cuando la cédula no tiene título
    if _FRASE_NO_ENCONTRADO_EXACTA in texto_pagina:
        return {
            "cedula":        cedula,
            "nombre":        None,
            "tiene_titulo":  False,
            "titulo":        None,
            "especialidad":  None,
            "institucion":   None,
            "fecha_grado":   None,
            "estado":        "NO_ENCONTRADO",
            "detalle":       "No existe registro de título de bachiller para esta cédula",
        }

    tabla = page.locator("table.rf-dt").first
    if tabla.count() == 0:
        return {
            "cedula":       cedula,
            "tiene_titulo": None,
            "titulo":       None,
            "especialidad": None,
            "institucion":  None,
            "fecha_grado":  None,
            "estado":       "PARSE_ERROR",
            "detalle":      "No se encontró la tabla de resultados (table.rf-dt)",
        }

    filas = tabla.locator("tbody tr").all()
    filas = [f for f in filas if f.inner_text().strip()]

    if not filas:
        return {
            "cedula":       cedula,
            "tiene_titulo": None,
            "titulo":       None,
            "especialidad": None,
            "institucion":  None,
            "fecha_grado":  None,
            "estado":       "PARSE_ERROR",
            "detalle":      "Tabla encontrada pero sin filas de datos",
        }

    celdas = filas[0].locator("td").all_inner_texts()
    celdas = [c.strip() for c in celdas]

    return {
        "cedula":       cedula,
        "nombre":       celdas[2] if len(celdas) > 2 else None,
        "tiene_titulo": True,
        "titulo":       celdas[4] if len(celdas) > 4 else (celdas[0] if celdas else "—"),
        "especialidad": celdas[5] if len(celdas) > 5 else None,
        "institucion":  celdas[3] if len(celdas) > 3 else None,
        "fecha_grado":  celdas[6] if len(celdas) > 6 else None,
        "estado":       "ENCONTRADO",
        "detalle":      " | ".join(c for c in celdas if c),
    }


# ── Un intento de consulta (sin abrir/cerrar browser) ────────────────────────

def _intentar_consulta(page, cedula: str, num: str) -> dict | None:
    """
    Carga la página, resuelve el captcha y envía el formulario.
    Devuelve el resultado o None si el captcha fue rechazado.
    """
    page.goto(MINISTERIO_URL, wait_until="domcontentloaded", timeout=60_000)

    img_captcha = page.locator(
        "img[src*='Captcha'], img[src*='captcha'], img[src*='kaptcha']"
    ).first
    img_captcha.wait_for(state="visible", timeout=10_000)
    captcha_b64 = base64.b64encode(img_captcha.screenshot()).decode()

    captcha_texto = _resolver_captcha(captcha_b64)
    print(f"[verificador] {cedula} — {num} captcha: '{captcha_texto}'")

    _campo_cedula(page).fill(cedula)
    _campo_captcha(page).fill(captcha_texto)
    _boton_buscar(page).click()

    try:
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
    except PlaywrightTimeout:
        pass

    texto_body = page.inner_text("body").lower()
    if any(f in texto_body for f in _FRASES_CAPTCHA_MAL):
        print(f"[verificador] {cedula} — captcha rechazado explícitamente")
        return None

    return _extraer_resultado(page, cedula)


# ── Función pública ───────────────────────────────────────────────────────────

def consultar_cedula(cedula: str, max_intentos: int = 4) -> dict:
    """
    Consulta en el Ministerio de Educación si una cédula tiene título de bachiller.

    Lógica de reintentos:
    - Hasta max_intentos intentos para superar el captcha.
    - Si el resultado es NO_ENCONTRADO, se hace un segundo intento de confirmación:
        · Segundo intento también NO_ENCONTRADO → confirmado sin título.
        · Segundo intento ENCONTRADO → el primero fue falso negativo (captcha malo
          que el servidor procesó sin dar error explícito).

    Thread-safety: el semáforo BACHILLER_SEMAPHORE limita la concurrencia
    para evitar el race condition de sync_playwright().
    """
    with _BACHILLER_SEMAPHORE:
        return _consultar_cedula_impl(cedula, max_intentos)


def _consultar_cedula_impl(cedula: str, max_intentos: int = 4) -> dict:
    """Implementación interna (no thread-safe — protegida por el semáforo arriba)."""
    # Proxy residencial opcional (para VPS con IP de datacenter bloqueada).
    # Orden de preferencia:
    #   1. BACHILLER_PROXY_URL/USER/PASS (formato separado, como Fiscalia)
    #   2. WEBSHARE_PROXY_URL (legacy, URL con auth inline)
    bach_url  = os.getenv("BACHILLER_PROXY_URL", "").strip()
    bach_user = os.getenv("BACHILLER_PROXY_USER", "").strip()
    bach_pass = os.getenv("BACHILLER_PROXY_PASS", "").strip()
    if bach_url:
        proxy_cfg = {"server": bach_url}
        if bach_user: proxy_cfg["username"] = bach_user
        if bach_pass: proxy_cfg["password"] = bach_pass
        print(f"[verificador] Usando proxy: {bach_url} user={bach_user[:20] if bach_user else '(sin user)'}")
    else:
        # Fallback al formato viejo WEBSHARE_PROXY_URL
        proxy_url_old = os.getenv("WEBSHARE_PROXY_URL", "").strip()
        if proxy_url_old:
            from urllib.parse import urlparse
            _p = urlparse(proxy_url_old)
            proxy_cfg = {
                "server":   f"{_p.scheme}://{_p.hostname}:{_p.port}",
                "username": _p.username,
                "password": _p.password,
            }
            print(f"[verificador] Usando proxy legacy WEBSHARE: {_p.hostname}:{_p.port}")
        else:
            proxy_cfg = None
            print("[verificador] Sin proxy — conectando directo")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            proxy=proxy_cfg,
        )
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()

        # Bloquear recursos innecesarios para ahorrar ancho de banda del proxy.
        # Se permite: document, script (JSF lo necesita), xhr/fetch (AJAX), y
        # las imágenes cuya URL contiene 'captcha'/'kaptcha' (imagen del CAPTCHA).
        # Se bloquea: stylesheet, font, media, e imágenes decorativas.
        def _filtrar_recurso(route, request):
            rt = request.resource_type
            if rt in ("stylesheet", "font", "media"):
                route.abort()
                return
            if rt == "image":
                url = request.url.lower()
                if any(k in url for k in ("captcha", "kaptcha")):
                    route.continue_()
                else:
                    route.abort()
                return
            route.continue_()

        page.route("**/*", _filtrar_recurso)

        try:
            # ── Fase 1: obtener un resultado inicial ──────────────────────────
            resultado = None
            for i in range(1, max_intentos + 1):
                print(f"[verificador] {cedula} — intento {i}/{max_intentos}")
                resultado = _intentar_consulta(page, cedula, f"intento {i}")
                if resultado is not None:
                    break

            if resultado is None:
                return {
                    "cedula":       cedula,
                    "tiene_titulo": None,
                    "titulo":       None,
                    "especialidad": None,
                    "institucion":  None,
                    "fecha_grado":  None,
                    "estado":       "ERROR_CAPTCHA",
                    "detalle":      f"No se pudo resolver el captcha en {max_intentos} intentos",
                }

            # ── Fase 2: doble confirmación de NO_ENCONTRADO ───────────────────
            # El Ministerio devuelve "no existe registro" tanto cuando el captcha
            # es incorrecto (silenciosamente aceptado) como cuando la cédula
            # genuinamente no tiene título. Requerimos 2 confirmaciones
            # independientes antes de marcar definitivamente como NO_ENCONTRADO.
            if resultado["estado"] == "NO_ENCONTRADO":
                print(f"[verificador] {cedula} — NO_ENCONTRADO en intento inicial, iniciando 2 confirmaciones...")
                confirmados_no = 0
                for i in range(1, 4):   # hasta 3 intentos de confirmación (con captcha válido)
                    conf = _intentar_consulta(page, cedula, f"confirmacion {i}")
                    if conf is None:
                        continue        # captcha rechazado, no cuenta
                    if conf["estado"] == "ENCONTRADO":
                        print(f"[verificador] {cedula} — ENCONTRADO en confirmacion {i} (intento inicial era falso negativo)")
                        return conf
                    confirmados_no += 1
                    print(f"[verificador] {cedula} — confirmacion {i}: NO_ENCONTRADO ({confirmados_no}/2)")
                    if confirmados_no >= 2:
                        print(f"[verificador] {cedula} — NO_ENCONTRADO confirmado por 2 intentos independientes")
                        return conf
                # Si no se acumularon 2 confirmaciones (solo captcha fallidos), conservar resultado original
                print(f"[verificador] {cedula} — no se completaron 2 confirmaciones; devolviendo NO_ENCONTRADO sin confirmar")
                resultado["detalle"] += " [sin confirmar — captcha falló en confirmaciones]"

            return resultado

        except Exception as e:
            print(f"[verificador] {cedula} — error inesperado: {e}")
            return {
                "cedula":       cedula,
                "tiene_titulo": None,
                "titulo":       None,
                "especialidad": None,
                "institucion":  None,
                "fecha_grado":  None,
                "estado":       "ERROR",
                "detalle":      str(e),
            }
        finally:
            browser.close()
