FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY simulador.py .
ENV FLUYE_DB=/datos/fluye.db
RUN mkdir -p /datos
EXPOSE 8000
CMD ["uvicorn", "app.main:crear_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
