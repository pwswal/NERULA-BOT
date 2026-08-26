FROM python:3.11-slim

WORKDIR /app

COPY nerula/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nerula/ ./nerula/

WORKDIR /app/nerula

RUN mkdir -p data

EXPOSE 8000

CMD ["python3", "main.py"]
