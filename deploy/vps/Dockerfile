# TODO: scaffold non ancora testato — vedi TODO.md
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rentwatch/ rentwatch/
COPY config.toml .

# data/ e reports/ vivono nei volumi (vedi docker-compose.yml)
CMD ["python", "-m", "rentwatch", "serve", "--host", "0.0.0.0", "--port", "8777"]
