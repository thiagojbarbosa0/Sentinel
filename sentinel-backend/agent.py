"""
Agente remoto do Sentinel (seção 22 — "multi-dispositivo").

Roda em qualquer máquina que você queira monitorar, coleta métricas locais
com os mesmos coletores do backend, e envia (`POST /ingest`) para um
Sentinel central rodando em outro host. Não depende de FastAPI/uvicorn —
só de `requests` e do módulo `app.collectors`, então pode ser instalado
isoladamente numa máquina que só tem o papel de "agente", sem servidor.

Uso:
    pip install requests psutil
    SENTINEL_SERVER_URL=http://192.168.1.10:8000 \
    SENTINEL_API_KEY=uma-chave-forte \
    python agent.py

Variáveis de ambiente:
    SENTINEL_SERVER_URL   URL do backend central (obrigatório)
    SENTINEL_API_KEY      Mesma chave configurada no backend, se houver
    SENTINEL_HOST_NAME    Identificador deste host (padrão: hostname da máquina)
    SENTINEL_INTERVAL     Segundos entre envios (padrão: 5)
"""
from __future__ import annotations

import os
import socket
import sys
import time

import requests

from app import collectors

SERVER_URL = os.getenv("SENTINEL_SERVER_URL", "").rstrip("/")
API_KEY = os.getenv("SENTINEL_API_KEY", "")
HOST_NAME = os.getenv("SENTINEL_HOST_NAME", socket.gethostname())
INTERVAL = float(os.getenv("SENTINEL_INTERVAL", "5"))


def build_payload() -> dict:
    system = collectors.collect_system()
    io_rates = collectors.collect_io_rates()
    network = collectors.collect_network_latency()
    processes = collectors.collect_processes()
    return {
        "host_id": HOST_NAME,
        "system": system,
        "io_rates": io_rates,
        "network": network,
        "processes": processes,
        "process_count": len(processes),
    }


def send(payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    res = requests.post(f"{SERVER_URL}/ingest", json=payload, headers=headers, timeout=10)
    if res.status_code >= 400:
        print(f"[sentinel-agent] servidor retornou {res.status_code}: {res.text}", file=sys.stderr)


def main() -> None:
    if not SERVER_URL:
        print("Defina SENTINEL_SERVER_URL apontando para o backend central.", file=sys.stderr)
        sys.exit(1)

    print(f"[sentinel-agent] host '{HOST_NAME}' enviando para {SERVER_URL} a cada {INTERVAL}s")

    # warm-up: cpu_percent por processo precisa de uma primeira chamada de
    # referência, senão os valores vêm todos como 0.0 na primeira leitura
    collectors.collect_processes()
    collectors.collect_system()
    time.sleep(0.5)

    while True:
        try:
            send(build_payload())
        except requests.RequestException as e:
            print(f"[sentinel-agent] falha ao enviar: {e}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
