"""
Configurações centrais do Sentinel.

Mantidas em um único lugar para que thresholds de anomalia, intervalos de
coleta e caminhos usados pelas ações de automação possam ser ajustados sem
mexer na lógica de negócio.
"""
import os
import socket
from pathlib import Path

# --- identidade do host ---
# Todo dado coletado é marcado com um host_id. O backend embutido sempre
# roda como o host 'local'; agentes remotos (seção 22) se identificam com o
# próprio hostname (ou um nome customizado) ao enviar dados via /ingest.
LOCAL_HOST_ID = "local"
LOCAL_HOST_DISPLAY_NAME = os.getenv("SENTINEL_HOST_NAME", socket.gethostname())

# --- coleta ---
COLLECT_INTERVAL_SECONDS = float(os.getenv("SENTINEL_INTERVAL", "2.0"))
HISTORY_WINDOW = int(os.getenv("SENTINEL_WINDOW", "120"))   # amostras mantidas em memória p/ baseline
TOP_N_PROCESSES = int(os.getenv("SENTINEL_TOP_N", "12"))    # processos monitorados em detalhe a cada tick

# Caminho usado para medir uso de disco. Rodando direto no host, "/" já é o
# que se quer. Rodando em Docker, "/" dentro do container é o filesystem do
# container, não o do host — nesse caso, monte o root do host em algo como
# /hostfs (read-only) e aponte esta variável para lá (ver docker-compose.yml).
DISK_PATH = os.getenv("SENTINEL_DISK_PATH", "/")

# --- detecção de anomalias ---
MIN_SAMPLES_FOR_BASELINE = 20      # amostras mínimas antes de calcular z-score
Z_SCORE_THRESHOLD = 3.0            # desvios padrão para considerar anomalia de métrica
PROCESS_ANOMALY_RATIO = 4.0        # processo precisa estar N vezes acima da própria baseline
PROCESS_ANOMALY_MIN_CPU = 15.0     # e acima desse piso absoluto de CPU (%) para não disparar ruído
EWMA_ALPHA = 0.15                  # peso do valor mais recente na baseline por processo

# --- banco de dados ---
DB_PATH = Path(os.getenv("SENTINEL_DB", str(Path(__file__).resolve().parent.parent / "sentinel.db")))

# Se SENTINEL_DB_URL estiver definida, ela tem prioridade e pode apontar
# para PostgreSQL/TimescaleDB (ex: postgresql+psycopg2://user:senha@host/db).
# Sem ela, cai para SQLite local em DB_PATH — é o padrão para uso simples/
# desenvolvimento. `app/database.py` usa SQLAlchemy Core para que a mesma
# lógica funcione contra os dois bancos sem branching por todo o código.
DB_URL = os.getenv("SENTINEL_DB_URL", f"sqlite:///{DB_PATH}")

# --- automação ---
# Nível global de autonomia (pode ser alterado em runtime via API):
#   1 = observação | 2 = recomendação | 3 = aprovação manual | 4 = automação
DEFAULT_AUTONOMY_LEVEL = int(os.getenv("SENTINEL_AUTONOMY", "3"))

# diretório "gerenciado" onde a ação de limpeza tem permissão de apagar arquivos.
# Nunca aponta para um diretório de sistema real — evita que uma automação
# apague algo fora do escopo demonstrado.
MANAGED_TEMP_DIR = Path(os.getenv("SENTINEL_TEMP_DIR", str(Path(__file__).resolve().parent.parent / "managed_tmp")))

# hosts usados no diagnóstico de rede (gateway é resolvido dinamicamente)
DNS_PROBE_HOST = "8.8.8.8"
INTERNET_PROBE_HOST = "1.1.1.1"
PROBE_PORT = 53
PROBE_TIMEOUT = 1.5

# --- segurança da API ---
# Se definida, toda rota que muda estado do host (aprovar ação, alterar
# autonomia) passa a exigir o header `X-API-Key` com esse valor. Vazia por
# padrão para não travar o uso local/demo — defina antes de expor a API
# além de localhost.
API_KEY = os.getenv("SENTINEL_API_KEY", "")

# --- testes ---
# Usado pela suíte de testes para subir a API sem o loop de coleta em
# background (que rodaria indefinidamente e tornaria os testes não-determinísticos).
DISABLE_SCHEDULER = os.getenv("SENTINEL_DISABLE_SCHEDULER", "0") == "1"

# --- retenção de dados ---
# Sem isso o SQLite cresce indefinidamente (seção 19 aponta TimescaleDB para
# produção, mas mesmo lá a retenção seria configurada explicitamente).
# Métricas e amostras de processo são de alto volume e baixo valor individual
# após um tempo; anomalias/recomendações/ações são poucas e valiosas como
# histórico, por isso guardadas por mais tempo.
RETENTION_DAYS_METRICS = float(os.getenv("SENTINEL_RETENTION_DAYS_METRICS", "14"))
RETENTION_DAYS_EVENTS = float(os.getenv("SENTINEL_RETENTION_DAYS_EVENTS", "90"))
RETENTION_CHECK_INTERVAL_SECONDS = float(os.getenv("SENTINEL_RETENTION_INTERVAL", str(3600)))

# --- detecção multivariada (ML) ---
ML_ENABLED = os.getenv("SENTINEL_ML_ENABLED", "1") == "1"
ML_MIN_SAMPLES = int(os.getenv("SENTINEL_ML_MIN_SAMPLES", "40"))   # amostras antes do 1º treino
ML_RETRAIN_EVERY = int(os.getenv("SENTINEL_ML_RETRAIN_EVERY", "20"))  # re-treina a cada N amostras novas
ML_WINDOW = int(os.getenv("SENTINEL_ML_WINDOW", "300"))            # amostras mantidas para treino
ML_CONTAMINATION = float(os.getenv("SENTINEL_ML_CONTAMINATION", "0.05"))  # fração esperada de outliers
