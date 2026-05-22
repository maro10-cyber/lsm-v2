FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app

# Default: paper trading mode
CMD ["python", "scripts/run_paper.py", "--config", "configs/config.yaml"]
