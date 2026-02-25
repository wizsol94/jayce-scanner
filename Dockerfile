FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
WORKDIR /app

# Force fresh install - separate layer for chart packages
RUN pip install --no-cache-dir mplfinance>=0.12.10 pandas>=2.0.0 matplotlib>=3.7.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify mplfinance is installed (build fails if not)
RUN python -c "import mplfinance; import pandas; import matplotlib; print('ALL PACKAGES OK')"

COPY scanner.py .
COPY railway.toml .
CMD ["python", "scanner.py"]
