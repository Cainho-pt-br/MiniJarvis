# Imagem do MiniJarvis Nuvem (a versão online).
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NUVEM_DB=/dados/nuvem.db
COPY nuvem/requirements.txt nuvem/requirements.txt
RUN pip install --no-cache-dir -r nuvem/requirements.txt
COPY nuvem nuvem
RUN mkdir -p /dados && useradd --system jarvis && chown jarvis /dados
USER jarvis
VOLUME /dados
EXPOSE 8000
CMD ["sh", "-c", "uvicorn nuvem.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
