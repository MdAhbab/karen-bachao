FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Dependencies first so application edits do not invalidate the wheel cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY tests/public_cases.json ./tests/public_cases.json
COPY tests/scenarios.json ./tests/scenarios.json

# Run unprivileged. No secret is baked in: GEMINI_API_KEY arrives at runtime.
RUN useradd --create-home --uid 10001 gridwise && chown -R gridwise:gridwise /app
USER gridwise

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
