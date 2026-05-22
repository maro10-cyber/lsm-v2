FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app

ENTRYPOINT ["python", "scripts/run_backtest.py"]
CMD ["--help"]
