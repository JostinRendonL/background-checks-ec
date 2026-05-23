# 🛡️ Background Checks Ecuador

> API REST para verificación automática de antecedentes de personas en Ecuador.
> Consulta en paralelo el Ministerio de Educación (título de bachiller) y la Función Judicial (procesos judiciales) y devuelve un **semáforo de riesgo** consolidado.

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](#despliegue)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 📋 Tabla de contenidos
- [Qué hace](#-qué-hace)
- [Arquitectura](#-arquitectura)
- [Endpoints](#-endpoints)
- [Semáforo de riesgo](#-semáforo-de-riesgo)
- [Variables de entorno](#-variables-de-entorno)
- [Desarrollo local](#-desarrollo-local)
- [Despliegue](#-despliegue)
- [Cómo funciona cada módulo](#-cómo-funciona-cada-módulo)
- [Métricas reales](#-métricas-reales)

---

## 🎯 Qué hace

Recibe una cédula ecuatoriana de 10 dígitos y en **~10 segundos** consulta dos fuentes oficiales en paralelo:

| Módulo | Fuente | Devuelve |
|--------|--------|----------|
| 🎓 **Bachiller** | Ministerio de Educación | ¿Tiene título? Título, especialidad, institución, fecha de grado |
| ⚖️ **SATJE** | Función Judicial | Procesos judiciales como demandado y como actor, con delito y fecha |

Consolidados en un **semáforo de riesgo** (ROJO/AMARILLO/VERDE/GRIS) listo para integrar con cualquier flujo de RR.HH., onboarding o n8n.

---

## 🏗️ Arquitectura

```
┌────────────────────────────────────────────────────────────┐
│                  Cliente (n8n / RR.HH. / etc.)             │
└────────────────┬───────────────────────────────────────────┘
                 │ POST /consultar/completo
                 │ { "cedula": "0912345675" }
                 ▼
┌────────────────────────────────────────────────────────────┐
│             FastAPI (api/main.py) — puerto 8000            │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  Semáforo de concurrencia (asyncio.Semaphore)       │  │
│   └─────────────────────────────────────────────────────┘  │
│                            │                                │
│              ┌─────────────┴─────────────┐                  │
│              ▼                           ▼                  │
│   ┌──────────────────┐         ┌──────────────────┐        │
│   │   BACHILLER      │         │      SATJE       │        │
│   │  Playwright +    │         │   httpx puro     │        │
│   │  Xvfb + Proxy    │         │   (API pública)  │        │
│   │  Ecuador +       │         │                  │        │
│   │  Gemini (OCR)    │         │                  │        │
│   └──────────────────┘         └──────────────────┘        │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌──────────────────────────┐
              │  Reporte consolidado     │
              │  + semáforo de riesgo    │
              └──────────────────────────┘
```

---

## 🔌 Endpoints

Todos los endpoints `/consultar/*` requieren header `X-API-Key: <BG_API_KEY>`.

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET`  | `/health` | Healthcheck (sin auth) |
| `POST` | `/consultar/bachiller` | Solo verificación de bachiller |
| `POST` | `/consultar/satje` | Solo procesos judiciales |
| `POST` | `/consultar/completo` | Bachiller + SATJE en paralelo + semáforo |
| `POST` | `/consultar/batch` | Lista de cédulas (máx 50) |
| `GET`  | `/docs` | Swagger UI interactivo |
| `GET`  | `/redoc` | ReDoc UI |

### Ejemplo: consulta completa

```bash
curl -X POST https://api.tudominio.com/consultar/completo \
  -H "Content-Type: application/json" \
  -H "X-API-Key: tu-api-key" \
  -d '{"cedula":"0912345675"}'
```

**Respuesta:**

```json
{
  "cedula": "0912345675",
  "semaforo": "VERDE",
  "bachiller": {
    "cedula": "0912345675",
    "tiene_titulo": true,
    "titulo": "Ciencias",
    "especialidad": "Físico Matemáticas",
    "institucion": "CARDENAL SPELLMAN LICEO",
    "fecha_grado": "1988-01-28",
    "estado": "ENCONTRADO"
  },
  "satje": {
    "cedula": "0912345675",
    "status": "SIN_PROCESOS",
    "total": 0,
    "total_demandado": 0,
    "total_actor": 0,
    "causas_demandado": [],
    "causas_actor": []
  },
  "tiempo_seg": 10.43
}
```

### Ejemplo: persona con antecedentes

```json
{
  "cedula": "1309022935",
  "semaforo": "ROJO",
  "bachiller": { "estado": "ENCONTRADO", "...": "..." },
  "satje": {
    "status": "CON_PROCESOS",
    "total": 19,
    "total_demandado": 13,
    "total_actor": 6,
    "causas_demandado": [
      { "idJuicio": "13284201700288", "delito": "140 ASESINATO, INC.1, NUM. 2", "fechaIngreso": "2017-03-02" },
      { "idJuicio": "13284201700410", "delito": "144 HOMICIDIO", "fechaIngreso": "2017-03-25" },
      { "idJuicio": "09286201701567", "delito": "369 DELINCUENCIA ORGANIZADA", "fechaIngreso": "2017-04-13" }
    ]
  },
  "tiempo_seg": 10.99
}
```

---

## 🚦 Semáforo de riesgo

| Color | Condición | Recomendación |
|-------|-----------|---------------|
| 🔴 **ROJO** | Tiene procesos judiciales como demandado | Riesgo alto — revisar manualmente |
| 🟡 **AMARILLO** | Sin título de bachiller O solo procesos como actor (víctima) | Riesgo medio — depende del puesto |
| 🟢 **VERDE** | Título OK y sin procesos | Sin alertas |
| ⚫ **GRIS** | Error en algún módulo (sin datos suficientes) | Reintentar |

> **Nota:** ROJO tiene prioridad absoluta. Aunque falle el módulo de Bachiller, si hay procesos como demandado se reporta ROJO.

---

## 🔐 Variables de entorno

| Variable | Obligatoria | Descripción |
|----------|-------------|-------------|
| `BG_API_KEY` | ✅ | Clave para proteger los endpoints |
| `OPENROUTER_API_KEY` | ✅ | OpenRouter (Gemini 2.0 Flash) para resolver captcha del Ministerio de Educación |
| `WEBSHARE_PROXY_URL` | ✅ | Proxy residencial Ecuador. Formato: `http://USER-cc-ec:PASS@p.webshare.io:80` |
| `MAX_BATCH_SIZE` | ❌ | Máximo de cédulas por request batch (default: 50) |
| `SEMAPHORE` | ❌ | Concurrencia paralela (default: 3) |

Ver [`.env.example`](.env.example) para plantilla completa.

---

## 💻 Desarrollo local

```bash
# 1. Clonar
git clone https://github.com/TU_USUARIO/background-checks-ec.git
cd background-checks-ec

# 2. Entorno virtual
python -m venv venv
source venv/bin/activate    # Linux/Mac
# venv\Scripts\activate     # Windows

# 3. Instalar dependencias
pip install -r requirements.txt
playwright install chromium --with-deps

# 4. Configurar .env
cp .env.example .env
# Editar .env con tus credenciales reales

# 5. Correr
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 6. Probar
curl http://localhost:8000/health
```

Abre http://localhost:8000/docs para el Swagger interactivo.

---

## 🚀 Despliegue

### Docker (recomendado)

```bash
docker build -t background-checks-ec .
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  --name bg-api \
  background-checks-ec
```

### Easypanel / Coolify / Dokploy

El `Dockerfile` está optimizado para auto-deploy desde Git. Solo conecta este repo al panel y agrega las variables de entorno desde la UI.

### VPS con systemd (sin Docker)

Ver instrucciones extendidas en `docs/DEPLOY_VPS.md` *(opcional)*.

---

## 🔎 Cómo funciona cada módulo

### 🎓 Bachiller — Ministerio de Educación

- **URL:** `servicios.educacion.gob.ec/titulacion25-web/...`
- **Reto:** captcha de imagen + bloqueo de IPs no ecuatorianas
- **Solución:**
  - Proxy residencial con IPs de Ecuador (WebShare, formato `-cc-ec`)
  - OCR del captcha con **Gemini 2.0 Flash** (~$0.0001 por captcha)
  - Doble confirmación de `NO_ENCONTRADO` para evitar falsos negativos por captcha mal resuelto
- **Precisión:** ~95% (validado con 141 empleados reales)
- **Tiempo:** ~8-12 segundos por consulta

### ⚖️ SATJE — Función Judicial

- **URL:** `api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-SERVICE/...`
- **Reto:** ninguno (API pública)
- **Solución:** `httpx` puro, sin browser
- **Tiempo:** ~0.7-3.6 segundos por consulta
- **Detalle:** Busca la cédula como **demandado/procesado** y como **actor/ofendido** por separado, paginando resultados con `contarCausas` + `buscarCausas`.

---

## 📊 Métricas reales

Validado en producción con cédulas reales:

| Caso | Resultado | Tiempo |
|------|-----------|--------|
| Persona limpia | VERDE, bachiller + 0 procesos | 10.4 s |
| Persona con antecedentes graves | ROJO, 13 procesos como demandado (asesinato, homicidio, delincuencia organizada) | 11.0 s |
| Sin título de bachiller | AMARILLO | 9.8 s |

---

## 🛣️ Roadmap

- [x] Módulo Bachiller (Ministerio de Educación)
- [x] Módulo SATJE (Función Judicial)
- [x] Semáforo de riesgo ROJO/AMARILLO/VERDE/GRIS
- [x] Endpoint batch con concurrencia controlada
- [x] Dockerfile listo para Easypanel
- [ ] Módulo SENESCYT (títulos universitarios) — *en evaluación*
- [ ] Reporte Excel/PDF consolidado por lote
- [ ] Workflow n8n de referencia
- [ ] Caché Redis (evitar reconsultas del mismo día)

---

## 📄 Licencia

MIT — uso libre con atribución. Ver [LICENSE](LICENSE).

---

## ⚠️ Disclaimer

Este proyecto consulta **únicamente fuentes públicas oficiales** del Estado ecuatoriano. No almacena información de las personas consultadas. El uso debe respetar la Ley Orgánica de Protección de Datos Personales del Ecuador (LOPDP).
