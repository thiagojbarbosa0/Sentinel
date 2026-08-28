"""
API do Sentinel (seção 4: "API / Backend").

REST para consultas e ações; WebSocket para o dashboard receber o snapshot
mais recente em tempo real sem fazer polling.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import automation, config, database as db
from app.scheduler import process_sample, retention_loop, run_forever, state

_ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    state.load_persisted_settings()
    tasks = []
    if not config.DISABLE_SCHEDULER:
        tasks = [
            asyncio.create_task(run_forever()),
            asyncio.create_task(_broadcast_loop()),
            asyncio.create_task(retention_loop()),
        ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Sentinel API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # protótipo local — restrinja em produção
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    Protege rotas que alteram o host (aprovar ação, mudar autonomia).
    Sem efeito se SENTINEL_API_KEY não estiver definida — mantém o uso local
    simples, mas fecha a porta assim que a API sai de localhost.
    """
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(401, "X-API-Key ausente ou inválida")


# ------------------------------------------------------------------ REST --

@app.get("/health")
def health():
    return {"status": "ok", "uptime_seconds": round(time.time() - state.started_at, 1)}


@app.get("/hosts")
def hosts():
    """Inventário de hosts conhecidos (local + agentes remotos que já enviaram dados)."""
    return db.list_hosts()


@app.get("/snapshot")
def snapshot(host_id: str = config.LOCAL_HOST_ID):
    """Estado atual completo de um host — o que o dashboard quer no load inicial."""
    snap = state.snapshots.get(host_id)
    if not snap:
        raise HTTPException(503, "Ainda coletando a primeira amostra deste host, tente novamente em instantes.")
    return snap


@app.get("/metrics/history")
def metrics_history(limit: int = 120, host_id: str = config.LOCAL_HOST_ID):
    return db.recent_metrics(limit=min(limit, 1000), host_id=host_id)


@app.get("/processes")
def processes(host_id: str = config.LOCAL_HOST_ID):
    return state.snapshots.get(host_id, {}).get("processes", [])


@app.get("/processes/history")
def process_history(name: str, limit: int = 200):
    rows = db.fetch_all(
        "SELECT ts, cpu, ram, baseline_cpu, is_anomaly FROM process_samples "
        "WHERE name = ? ORDER BY ts DESC LIMIT ?", (name, min(limit, 1000)),
    )
    return list(reversed(rows))


@app.get("/network")
def network(host_id: str = config.LOCAL_HOST_ID):
    return state.snapshots.get(host_id, {}).get("network", {})


@app.get("/anomalies")
def anomalies(status: str | None = None, host_id: str = config.LOCAL_HOST_ID):
    if status:
        return db.fetch_all(
            "SELECT * FROM anomalies WHERE host_id=? AND status=? ORDER BY ts DESC", (host_id, status),
        )
    return db.fetch_all("SELECT * FROM anomalies WHERE host_id=? ORDER BY ts DESC LIMIT 50", (host_id,))


@app.get("/recommendations")
def recommendations(status: str | None = "pending", host_id: str = config.LOCAL_HOST_ID):
    if status:
        return db.fetch_all(
            "SELECT * FROM recommendations WHERE host_id=? AND status=? ORDER BY ts DESC", (host_id, status),
        )
    return db.fetch_all("SELECT * FROM recommendations WHERE host_id=? ORDER BY ts DESC LIMIT 50", (host_id,))


@app.get("/actions")
def actions(limit: int = 50, host_id: str = config.LOCAL_HOST_ID):
    return db.fetch_all(
        "SELECT * FROM actions WHERE host_id=? ORDER BY ts DESC LIMIT ?", (host_id, min(limit, 500)),
    )


@app.get("/events")
def events(limit: int = 50, host_id: str | None = None):
    return db.recent_events(limit=min(limit, 500), host_id=host_id)


class IngestPayload(BaseModel):
    host_id: str
    system: dict
    io_rates: dict = {}
    network: dict = {}
    processes: list[dict] = []
    process_count: int | None = None


@app.post("/ingest", dependencies=[Depends(require_api_key)])
async def ingest(payload: IngestPayload):
    """
    Recebe uma amostra de um agente remoto (ver `agent.py`) e roda a mesma
    pipeline de detecção/diagnóstico/recomendação usada para o host local —
    cada host com seu próprio baseline (seção 22: multi-dispositivo).

    Diferente do host local, ações de automação NUNCA são auto-executadas
    aqui, mesmo em N4: o backend não tem como controlar processos de uma
    máquina remota sem um agente de execução lá (ver README).
    """
    if payload.host_id == config.LOCAL_HOST_ID:
        raise HTTPException(400, f"'{config.LOCAL_HOST_ID}' é reservado para o host onde a API roda")
    snap = await process_sample(
        payload.host_id, payload.system, payload.io_rates, payload.network, payload.processes,
        process_count=payload.process_count or len(payload.processes), is_local=False,
    )
    return {"received": True, "health_score": snap["health_score"], "diagnosis": snap["diagnosis"]}


@app.get("/storage/stats")
def storage_stats():
    return db.storage_stats()


@app.post("/storage/purge", dependencies=[Depends(require_api_key)])
def storage_purge(metrics_days: float | None = None, events_days: float | None = None):
    """Dispara a retenção manualmente, sem esperar o ciclo periódico."""
    from app.config import RETENTION_DAYS_EVENTS, RETENTION_DAYS_METRICS
    now = time.time()
    deleted = db.purge_old_data(
        metrics_cutoff_ts=now - (metrics_days if metrics_days is not None else RETENTION_DAYS_METRICS) * 86400,
        events_cutoff_ts=now - (events_days if events_days is not None else RETENTION_DAYS_EVENTS) * 86400,
    )
    db.vacuum()
    db.insert_event("info", f"Purga manual: {sum(deleted.values())} registros removidos.", deleted)
    return {"deleted": deleted, "stats_after": db.storage_stats()}


class RecommendationDecision(BaseModel):
    pass


@app.post("/recommendations/{rec_id}/approve", dependencies=[Depends(require_api_key)])
def approve_recommendation(rec_id: int):
    rec = db.fetch_one("SELECT * FROM recommendations WHERE id=?", (rec_id,))
    if not rec:
        raise HTTPException(404, "Recomendação não encontrada")
    if rec["status"] != "pending":
        raise HTTPException(409, f"Recomendação já está em status '{rec['status']}'")
    if rec["host_id"] != config.LOCAL_HOST_ID:
        raise HTTPException(
            422,
            f"Ação em host remoto ('{rec['host_id']}') não é suportada nesta versão — "
            "requer um agente de execução rodando naquele host. Aplique a ação manualmente lá.",
        )

    result = automation.execute_action(rec["action_type"], rec["target"])
    db.insert_action({
        "ts": time.time(), "host_id": rec["host_id"], "recommendation_id": rec_id, "action_type": rec["action_type"],
        "target": rec["target"], "risk": rec["risk"], "status": result["status"],
        "before": result["before"], "after": result["after"], "rollback": result["rollback"],
        "error": result["error"],
    })
    db.update_recommendation_status(rec_id, "executed" if result["status"] == "executed" else "failed")
    if rec["anomaly_id"]:
        db.update_anomaly_status(rec["anomaly_id"], "resolved" if result["status"] == "executed" else "diagnosed")
    level = "success" if result["status"] == "executed" else "critical"
    db.insert_event(level, f"Usuário aprovou ação '{rec['action_type']}' → {result['status']}",
                      {"recommendation_id": rec_id}, host_id=rec["host_id"])
    return result


@app.post("/recommendations/{rec_id}/dismiss", dependencies=[Depends(require_api_key)])
def dismiss_recommendation(rec_id: int):
    rec = db.fetch_one("SELECT * FROM recommendations WHERE id=?", (rec_id,))
    if not rec:
        raise HTTPException(404, "Recomendação não encontrada")
    db.update_recommendation_status(rec_id, "dismissed")
    if rec["anomaly_id"]:
        db.update_anomaly_status(rec["anomaly_id"], "dismissed")
    db.insert_event("info", f"Recomendação #{rec_id} descartada pelo usuário")
    return {"status": "dismissed"}


class AutonomyLevel(BaseModel):
    level: int


@app.post("/autonomy", dependencies=[Depends(require_api_key)])
def set_autonomy(payload: AutonomyLevel):
    if payload.level not in (1, 2, 3, 4):
        raise HTTPException(400, "Nível deve ser 1, 2, 3 ou 4")
    state.set_autonomy(payload.level)
    db.insert_event("info", f"Nível de autonomia alterado para N{payload.level}")
    return {"autonomy_level": state.autonomy_level}


@app.get("/autonomy")
def get_autonomy():
    return {"autonomy_level": state.autonomy_level}


# ------------------------------------------------------------------- WS ---

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        if state.latest_snapshot:
            await ws.send_json(state.latest_snapshot)
        while True:
            await ws.receive_text()  # mantém a conexão viva; ignora mensagens do cliente
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


async def _broadcast_loop():
    last_ts = None
    while True:
        await asyncio.sleep(1.0)
        snap = state.latest_snapshot
        if not snap or snap.get("ts") == last_ts:
            continue
        last_ts = snap.get("ts")
        dead = []
        for ws in _ws_clients:
            try:
                await ws.send_json(snap)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)
