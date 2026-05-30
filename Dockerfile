FROM python:3.12-slim

# Dependencias del sistema: Playwright/Chromium + Xvfb (necesario para módulo Bachiller)
RUN apt-get update && apt-get install -y \
    xvfb curl wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libx11-6 libxcb1 libxext6 libxfixes3 libxi6 libxrender1 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium de Playwright (reusado por patchright tambien)
RUN playwright install chromium --with-deps
# Patchright = fork de Playwright con stealth fuerte. Reusa el mismo Chromium
# pero con parchos anti-detection. Solo necesita el browser ya instalado.
RUN patchright install chromium 2>/dev/null || true

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Xvfb necesario para Playwright en modo "headless visual" (pasa Incapsula/Imperva)
# Módulo SATJE usa solo httpx (sin browser)
CMD ["sh", "-c", "Xvfb :99 -screen 0 1366x768x24 -ac & export DISPLAY=:99 && uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1"]
