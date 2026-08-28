"""
Loop de coleta (o "agente" da seção 5 rodando embutido no processo do
backend para o host local, por simplicidade — em produção seria um processo
separado enviando métricas via HTTP/gRPC para a API, como no diagrama da
seção 4).

A pipeline por amostra é a mesma independentemente da origem dos dados:
    coletar → pontuar anomalias → correlacionar/diagnosticar →
    recomendar (ou auto-executar, conforme nível de autonomia) → persistir

`process_sample()` implementa essa pipeline de forma agnóstica à origem, para
que tanto o host local (via `tick()`) quanto agentes remotos (via
`POST /ingest` na API) passem pelo mesmo motor de detecção — cada host com
seu próprio `AnomalyEngine`, para que o baseline de um não vaze pro outro
(seção 22: "multi-dispositivo").
"""
from __future__ import annotations

import asyncio
import time

from app import automation, collectors, database as db
from app.anomaly import AnomalyEngine, compute_health_score
from app.config import (
    COLLECT_INTERVAL_SECONDS,
    DEFAULT_AUTONOMY_LEVEL,
    LOCAL_HOST_DISPLAY_NAME,
    LOCAL_HOST_ID,
    RETENTION_CHECK_INTERVAL_SECONDS,
    RETENTION_DAYS_EVENTS,
    RETENTION_DAYS_METRICS,
)
from app.diagnostics import diagnose, recommend


class SentinelState:
    """
    Estado compartilhado exposto para a API.

    `engines` e `snapshots` são mantidos por host_id: cada host tem seu
    próprio motor de baseline estatístico e seu próprio snapshot mais
    recente. `autonomy_level` continua global — é uma configuração do
    operador do Sentinel, não do host monitorado.
    """

    def __init__(self):
        self.autonomy_level = DEFAULT_AUTONOMY_LEVEL
        self.engines: dict[str, AnomalyEngine] = {}
        self.snapshots: dict[str, dict] = {}
        self.started_at = time.time()
        self._open_diagnosis_types: dict[str, set[str]] = {}  # por host_id

    def load_persisted_settings(self) -> None:
        """Restaura configurações salvas entre reinícios (ex: nível de autonomia)."""
        saved = db.get_setting("autonomy_level")
        if saved is not None:
            try:
                self.autonomy_level = int(saved)
            except ValueError:
                pass

    def set_autonomy(self, level: int) -> None:
        self.autonomy_level = level
        db.set_setting("autonomy_level", str(level))

    def get_engine(self, host_id: str) -> AnomalyEngine:
        if host_id not in self.engines:
            self.engines[host_id] = AnomalyEngine()
            self._open_diagnosis_types[host_id] = set()
        return self.engines[host_id]

    @property
    def latest_snapshot(self) -> dict:
        """Compatibilidade: chamadores existentes que só conhecem 'o' snapshot pegam o do host local."""
        return self.snapshots.get(LOCAL_HOST_ID, {})


state = SentinelState()


async def _run_action_pipeline(host_id: str, is_local: bool, diagnosis: dict, anomaly_id: int) -> None:
    rec = recommend(diagnosis)
    if rec is None:
        return
    rec["anomaly_id"] = anomaly_id
    rec["host_id"] = host_id
    rec_id = db.insert_recommendation(rec)
    db.insert_event("warning", f"[{host_id}] Recomendação gerada: {rec['description']}",
                      {"recommendation_id": rec_id}, host_id=host_id)

    # Automação em N4 só é honrada para o host local: executar uma ação real
    # (renice, limpeza de arquivo) num host remoto exigiria um agente de
    # execução rodando lá, que está fora do escopo desta versão (seção 15 —
    # nunca executar algo que não se tem certeza de poder controlar).
    auto_execute = is_local and state.autonomy_level >= 4 and rec["risk"] in ("NONE", "LOW")
    if not auto_execute:
        return

    await asyncio.sleep(1.0)  # pequeno delay para simular preparo da ação
    result = automation.execute_action(rec["action_type"], rec["target"])
    db.insert_action({
        "ts": time.time(), "host_id": host_id, "recommendation_id": rec_id, "action_type": rec["action_type"],
        "target": rec["target"], "risk": rec["risk"], "status": result["status"],
        "before": result["before"], "after": result["after"], "rollback": result["rollback"],
        "error": result["error"],
    })
    db.update_recommendation_status(rec_id, "executed" if result["status"] == "executed" else "failed")
    level = "success" if result["status"] == "executed" else "critical"
    db.insert_event(level, f"[N4 automático] Ação '{rec['action_type']}' → {result['status']}",
                      {"recommendation_id": rec_id}, host_id=host_id)


async def process_sample(host_id: str, system: dict, io_rates: dict, network: dict,
                           processes: list[dict], process_count: int, is_local: bool) -> dict:
    """
    Pipeline única de detecção → diagnóstico → recomendação → persistência,
    compartilhada entre a coleta local (`tick`) e a ingestão remota
    (`POST /ingest`). Retorna o snapshot resultante para aquele host.
    """
    now = time.time()
    engine = state.get_engine(host_id)
    open_types = state._open_diagnosis_types.setdefault(host_id, set())

    scored_procs = engine.score_processes(processes)
    health = compute_health_score(system["cpu"], system["ram"], system["disk"], network.get("internet_latency_ms"))

    db.insert_metric({
        "host_id": host_id, "ts": now, "cpu": system["cpu"], "ram": system["ram"], "disk": system["disk"],
        "disk_read": io_rates.get("disk_read_mbs"), "disk_write": io_rates.get("disk_write_mbs"),
        "net_rx": io_rates.get("net_rx_mbs"), "net_tx": io_rates.get("net_tx_mbs"),
        "net_latency_gateway": network.get("gateway_latency_ms"), "net_latency_dns": network.get("dns_latency_ms"),
        "net_latency_internet": network.get("internet_latency_ms"),
        "process_count": process_count, "health_score": health,
    })
    db.insert_process_samples(host_id, now, scored_procs)
    db.upsert_host(host_id, display_name=LOCAL_HOST_DISPLAY_NAME if is_local else None, is_local=is_local)

    metric_anomalies = engine.score_metrics({
        "cpu": system["cpu"], "ram": system["ram"], "disk": system["disk"],
        "internet_latency_ms": network.get("internet_latency_ms"),
    })
    ml_signal = engine.score_multivariate({
        "cpu": system["cpu"], "ram": system["ram"], "disk": system["disk"],
        "internet_latency_ms": network.get("internet_latency_ms"),
    })

    diagnosis = None
    disk_free_gb = system.get("disk_free_gb", 999)
    if metric_anomalies or ml_signal or (disk_free_gb is not None and disk_free_gb < 5):
        diagnosis = diagnose(metric_anomalies, scored_procs, network, disk_free_gb, ml_signal)

    if diagnosis:
        if diagnosis["type"] not in open_types:
            open_types.add(diagnosis["type"])
            anomaly_id = db.insert_anomaly({
                "host_id": host_id, "ts": now, "metric": diagnosis["type"], "value": system["cpu"],
                "baseline_mean": metric_anomalies.get("cpu", {}).get("baseline_mean"),
                "baseline_std": metric_anomalies.get("cpu", {}).get("baseline_std"),
                "zscore": metric_anomalies.get("cpu", {}).get("zscore"),
                "severity": diagnosis["severity"], "diagnosis": diagnosis["cause"],
                "evidence": diagnosis["evidence"], "confidence": diagnosis["confidence"], "status": "diagnosed",
            })
            db.insert_event("critical", f"[{host_id}] Anomalia diagnosticada: {diagnosis['cause']} "
                                          f"(confiança {diagnosis['confidence']*100:.0f}%)",
                              {"anomaly_id": anomaly_id}, host_id=host_id)
            if state.autonomy_level >= 2:
                await _run_action_pipeline(host_id, is_local, diagnosis, anomaly_id)
    elif not metric_anomalies and not ml_signal:
        open_types.clear()  # sistema estável: libera os tipos p/ poderem disparar de novo no futuro

    snapshot = {
        "host_id": host_id, "ts": now, "system": system, "io_rates": io_rates, "network": network,
        "processes": scored_procs, "health_score": health,
        "metric_anomalies": metric_anomalies, "ml_signal": ml_signal, "diagnosis": diagnosis,
        "autonomy_level": state.autonomy_level,
    }
    state.snapshots[host_id] = snapshot
    return snapshot


async def tick() -> None:
    """Coleta um ciclo do host local e roda a pipeline compartilhada."""
    system = collectors.collect_system()
    io_rates = collectors.collect_io_rates()
    network = collectors.collect_network_latency()
    processes = collectors.collect_processes()
    await process_sample(
        LOCAL_HOST_ID, system, io_rates, network, processes,
        process_count=len(psutil_process_count()), is_local=True,
    )


def psutil_process_count():
    import psutil
    return psutil.pids()


async def run_forever() -> None:
    # warm-up: primeira chamada de cpu_percent por processo precisa de um
    # intervalo de referência, senão todos os valores vêm como 0.0
    collectors.collect_processes()
    collectors.collect_system()
    await asyncio.sleep(0.5)
    db.insert_event("success", "Sentinel inicializado. Estabelecendo perfil de comportamento normal.",
                      host_id=LOCAL_HOST_ID)
    while True:
        try:
            await tick()
        except Exception as e:  # nunca deixa o loop morrer por causa de um erro pontual de coleta
            db.insert_event("critical", f"Erro no ciclo de coleta: {e}", host_id=LOCAL_HOST_ID)
        await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


async def retention_loop() -> None:
    """
    Roda em paralelo ao loop de coleta, aplicando a política de retenção
    periodicamente para o SQLite não crescer sem limite (seção 19 aponta
    TimescaleDB para escala maior, mas mesmo lá a retenção é configurada
    explicitamente — isso não é algo que se "resolve" só trocando de banco).
    """
    while True:
        await asyncio.sleep(RETENTION_CHECK_INTERVAL_SECONDS)
        try:
            now = time.time()
            deleted = db.purge_old_data(
                metrics_cutoff_ts=now - RETENTION_DAYS_METRICS * 86400,
                events_cutoff_ts=now - RETENTION_DAYS_EVENTS * 86400,
            )
            total = sum(deleted.values())
            if total > 0:
                db.insert_event("info", f"Retenção de dados: {total} registros antigos removidos.", deleted)
        except Exception as e:
            db.insert_event("critical", f"Erro no ciclo de retenção: {e}")
