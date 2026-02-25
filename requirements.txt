FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && echo "v3.3.2-charts"
COPY scanner.py .
COPY railway.toml .
CMD ["python", "scanner.py"]
