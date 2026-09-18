FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

RUN useradd --create-home appuser

WORKDIR /app

COPY requirements.txt .
# `chromium` covers the Apple/ASUS scrapers. Acer needs no browser at all —
# its pages are saved by hand and parsed offline from app/scraper/data/acer_html
# (the store's WAF refuses automated requests), so the real-Chrome install that
# used to live here is gone.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chown -R appuser:appuser /opt/playwright-browsers

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini .

# WORKDIR /app itself is root-owned; the app writes logs/ (logger.py,
# rag/evaluation.py, scraper failure logs) relative to it at runtime
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs

USER appuser

EXPOSE 8000

# ALEMBIC_TARGET=production is required, not incidental: alembic/env.py now
# defaults to the throwaway test database so that a developer running
# `alembic upgrade head` cannot reach Supabase by accident. The deploy is the
# one place that SHOULD migrate production, so it says so.
CMD ["sh", "-c", "ALEMBIC_TARGET=production alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]