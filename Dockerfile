# Two stages: build the React dashboard, then serve everything from the FastAPI image.
# Same-origin design (CLAUDE.md, "Section 6: one platform, no CORS") means the backend
# image must carry the dashboard's build output, not just its own source.

FROM node:22-alpine AS dashboard-build
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=dashboard-build /dashboard/dist ./dashboard/dist

# Railway injects $PORT at runtime; shell form so it expands.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
