FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mwaa_agent/ mwaa_agent/
COPY config/ config/
COPY apps/__init__.py apps/__init__.py
COPY apps/web/ apps/web/

# App Runner/ECS inject real env vars; no .env file is copied into the image
# on purpose - secrets belong in the platform's secret store, not the image.
EXPOSE 8000
CMD ["uvicorn", "apps.web.webapp:app", "--host", "0.0.0.0", "--port", "8000"]
