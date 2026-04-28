FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
  build-essential \
  libpq-dev \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# UI Lab: enable automatic screenshots (Playwright Chromium)
RUN python -m playwright install --with-deps chromium

COPY . /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Render sets $PORT at runtime. Use a single entrypoint script to avoid quoting issues.
CMD ["bash", "bin/start_web.sh"]
