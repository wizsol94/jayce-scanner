FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
WORKDIR /app
# v3.3.2-charts: added mplfinance pandas matplotlib
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner.py .
COPY railway.toml .
CMD ["python", "scanner.py"]
