FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY simulador.py .
ENV FLUYE_DB=/datos/fluye.db \
    TZ=America/Bogota
RUN mkdir -p /datos
EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.main:crear_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
