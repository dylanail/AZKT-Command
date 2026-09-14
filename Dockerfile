# AZKT — single image for the web/API service and the worker (Railway runs each as its own service).
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY . .
COPY --from=frontend /app/frontend/dist /app/frontend/dist
ENV API_HOST=0.0.0.0 API_PORT=8787 ENV=production
EXPOSE 8787
# Web/API: `python -m backend.app.main`; worker: `python -m backend.worker` (see railway.json)
CMD ["python", "-m", "backend.app.main"]
