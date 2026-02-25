FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install Python deps only (Playwright + Chromium already in base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code LAST — this is the only layer that changes on each deploy
COPY scanner.py .
COPY railway.toml .

CMD ["python", "scanner.py"]
