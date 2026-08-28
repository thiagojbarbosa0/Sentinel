# Imagem enxuta o bastante para não pesar, mas com scikit-learn/numpy
# funcionando de fábrica (evita ter que compilar nada em runtime).
FROM python:3.12-slim

WORKDIR /app

# psutil precisa de build tools em algumas plataformas para compilar sua
# extensão C; instala e remove na mesma camada para não inflar a imagem.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Para usar PostgreSQL em vez do SQLite padrão dentro do container, troque a
# linha acima por: pip install --no-cache-dir -r requirements.txt -r requirements-postgres.txt
# (e copie requirements-postgres.txt também) — não vem instalado por padrão
# para manter a imagem menor para quem só quer SQLite.

COPY app/ app/
COPY main.py .

# Caminhos de dados ficam num volume — ver docker-compose.yml.
ENV SENTINEL_DB=/data/sentinel.db
ENV SENTINEL_TEMP_DIR=/data/managed_tmp
RUN mkdir -p /data

EXPOSE 8000

# Sem --reload: isso é a imagem de execução, não de desenvolvimento.
CMD ["python", "main.py"]
