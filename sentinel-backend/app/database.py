"""
Camada de armazenamento.

Usa SQLAlchemy Core (não o ORM — não precisamos de classes de modelo, só de
executar SQL de forma portável) para que a MESMA lógica funcione tanto
contra SQLite local (padrão, zero configuração) quanto contra PostgreSQL
real (definindo `SENTINEL_DB_URL`). Isso cumpre o que a seção 19 do
documento original pede — TimescaleDB é uma extensão do PostgreSQL, então
um banco Postgres configurado aqui já pode ganhar hypertables depois sem
tocar neste arquivo de novo.

Cada tabela é "append-only" e indexada por (host_id, ts) — pensada como
série temporal desde o início, não como um banco relacional genérico.
"""
from __future__ import annotations

import json
import time
from typing import Iterable

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    text,
)

from app.config import DB_PATH, DB_URL

metadata = MetaData()

metrics = Table(
    "metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=False, server_default="local"),
    Column("ts", Float, nullable=False),
    Column("cpu", Float), Column("ram", Float), Column("disk", Float),
    Column("disk_read", Float), Column("disk_write", Float),
    Column("net_rx", Float), Column("net_tx", Float),
    Column("net_latency_gateway", Float), Column("net_latency_dns", Float), Column("net_latency_internet", Float),
    Column("process_count", Integer),
    Column("health_score", Float),
    Index("idx_metrics_host_ts", "host_id", "ts"),
)

process_samples = Table(
    "process_samples", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=False, server_default="local"),
    Column("ts", Float, nullable=False),
    Column("pid", Integer), Column("name", String(256)),
    Column("cpu", Float), Column("ram", Float),
    Column("baseline_cpu", Float),
    Column("is_anomaly", Integer, server_default="0"),
    Index("idx_proc_host_ts", "host_id", "ts"),
    Index("idx_proc_name", "name"),
)

anomalies = Table(
    "anomalies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=False, server_default="local"),
    Column("ts", Float, nullable=False),
    Column("metric", String(64), nullable=False),
    Column("value", Float), Column("baseline_mean", Float), Column("baseline_std", Float), Column("zscore", Float),
    Column("severity", String(32)),
    Column("diagnosis", Text),
    Column("evidence", Text),  # JSON list
    Column("confidence", Float),
    Column("status", String(32), server_default="open"),  # open | diagnosed | resolved | dismissed
    Index("idx_anom_host_ts", "host_id", "ts"),
)

recommendations = Table(
    "recommendations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=False, server_default="local"),
    Column("ts", Float, nullable=False),
    Column("anomaly_id", Integer),
    Column("action_type", String(64), nullable=False),
    Column("target", String(256)),
    Column("description", Text),
    Column("risk", String(16)),
    Column("expected_impact", Text),
    Column("status", String(32), server_default="pending"),  # pending | dismissed | executed | failed
    Index("idx_rec_host", "host_id"),
)

actions = Table(
    "actions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=False, server_default="local"),
    Column("ts", Float, nullable=False),
    Column("recommendation_id", Integer),
    Column("action_type", String(64), nullable=False),
    Column("target", String(256)),
    Column("risk", String(16)),
    Column("status", String(32)),  # executed | failed | rolled_back
    Column("before_json", Text), Column("after_json", Text), Column("rollback_json", Text),
    Column("error", Text),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("host_id", String(128), nullable=True),  # NULL = evento global do backend
    Column("ts", Float, nullable=False),
    Column("level", String(16), nullable=False),  # info | warning | critical | success
    Column("message", Text, nullable=False),
    Column("meta", Text),
    Index("idx_events_ts", "ts"),
)

settings = Table(
    "settings", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text),
)

hosts = Table(
    "hosts", metadata,
    Column("host_id", String(128), primary_key=True),
    Column("display_name", String(256)),
    Column("first_seen", Float),
    Column("last_seen", Float),
    Column("is_local", Integer, server_default="0"),
)

_TABLES_BY_NAME = {
    "metrics": metrics, "process_samples": process_samples, "anomalies": anomalies,
    "recommendations": recommendations, "actions": actions, "events": events,
}

_engine = None


def _make_engine():
    kwargs = {"pool_pre_ping": True}
    if DB_URL.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(DB_URL, **kwargs)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
        if _engine.dialect.name == "sqlite":
            # WAL reduz "database is locked" com o scheduler e a API escrevendo concorrentemente.
            with _engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
    return _engine


def init_db() -> None:
    metadata.create_all(get_engine())


def reset_db() -> None:
    """Recria o schema do zero — usado pelos testes para isolar cada execução."""
    engine = get_engine()
    metadata.drop_all(engine)
    metadata.create_all(engine)


def _to_dict(row) -> dict:
    return dict(row._mapping)


def _positional_to_named(query: str, args: tuple) -> tuple[str, dict]:
    """
    Todo o código deste projeto (e os testes) chama fetch_all/fetch_one com
    placeholders estilo SQLite ('?'). SQLAlchemy `text()` precisa de
    parâmetros nomeados para funcionar de forma idêntica em SQLite e
    PostgreSQL — então convertemos aqui, num único lugar, em vez de exigir
    que cada chamador soubesse o dialeto do banco.
    """
    named_query, params = query, {}
    for i, value in enumerate(args):
        key = f"p{i}"
        named_query = named_query.replace("?", f":{key}", 1)
        params[key] = value
    return named_query, params


# ---------------------------------------------------------------- inserts --

def insert_metric(row: dict) -> int:
    row = {**row, "host_id": row.get("host_id", "local")}
    with get_engine().begin() as conn:
        result = conn.execute(metrics.insert(), row)
        return result.inserted_primary_key[0]


def insert_process_samples(host_id: str, ts: float, samples: Iterable[dict]) -> None:
    rows = [
        {"host_id": host_id, "ts": ts, "pid": s["pid"], "name": s["name"], "cpu": s["cpu"], "ram": s["ram"],
         "baseline_cpu": s.get("baseline_cpu"), "is_anomaly": int(s.get("is_anomaly", False))}
        for s in samples
    ]
    if not rows:
        return
    with get_engine().begin() as conn:
        conn.execute(process_samples.insert(), rows)


def insert_anomaly(row: dict) -> int:
    row = {**row, "host_id": row.get("host_id", "local"),
           "evidence": json.dumps(row.get("evidence", []), ensure_ascii=False)}
    with get_engine().begin() as conn:
        result = conn.execute(anomalies.insert(), row)
        return result.inserted_primary_key[0]


def update_anomaly_status(anomaly_id: int, status: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(anomalies.update().where(anomalies.c.id == anomaly_id).values(status=status))


def insert_recommendation(row: dict) -> int:
    row = {**row, "host_id": row.get("host_id", "local")}
    with get_engine().begin() as conn:
        result = conn.execute(recommendations.insert(), row)
        return result.inserted_primary_key[0]


def update_recommendation_status(rec_id: int, status: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(recommendations.update().where(recommendations.c.id == rec_id).values(status=status))


def insert_action(row: dict) -> int:
    row = {
        **row,
        "host_id": row.get("host_id", "local"),
        "before_json": json.dumps(row.get("before"), ensure_ascii=False),
        "after_json": json.dumps(row.get("after"), ensure_ascii=False),
        "rollback_json": json.dumps(row.get("rollback"), ensure_ascii=False),
    }
    row = {k: v for k, v in row.items() if k in actions.c}
    with get_engine().begin() as conn:
        result = conn.execute(actions.insert(), row)
        return result.inserted_primary_key[0]


def get_setting(key: str, default: str | None = None) -> str | None:
    row = fetch_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    _upsert(
        settings, {"key": key, "value": value},
        index_elements=["key"], update_cols={"value": value},
    )


def insert_event(level: str, message: str, meta: dict | None = None, host_id: str | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(events.insert(), {
            "host_id": host_id, "ts": time.time(), "level": level,
            "message": message, "meta": json.dumps(meta or {}, ensure_ascii=False),
        })


def upsert_host(host_id: str, display_name: str | None = None, is_local: bool = False) -> None:
    """Registra/atualiza o 'último visto' de um host — base do inventário multi-dispositivo (seção 22)."""
    now = time.time()
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    stmt = dialect_insert(hosts).values(
        host_id=host_id, display_name=display_name, first_seen=now, last_seen=now, is_local=int(is_local),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["host_id"],
        set_={
            "last_seen": stmt.excluded.last_seen,
            "display_name": func.coalesce(stmt.excluded.display_name, hosts.c.display_name),
        },
    )
    with engine.begin() as conn:
        conn.execute(stmt)


def _upsert(table: Table, values: dict, index_elements: list[str], update_cols: dict) -> None:
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(table).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=update_cols)
    with engine.begin() as conn:
        conn.execute(stmt)


def list_hosts() -> list[dict]:
    return fetch_all("SELECT * FROM hosts ORDER BY is_local DESC, last_seen DESC")


# ---------------------------------------------------------------- queries --

def fetch_all(query: str, args: tuple = ()) -> list[dict]:
    named_query, params = _positional_to_named(query, args)
    with get_engine().begin() as conn:
        result = conn.execute(text(named_query), params)
        return [_to_dict(r) for r in result.fetchall()]


def fetch_one(query: str, args: tuple = ()) -> dict | None:
    named_query, params = _positional_to_named(query, args)
    with get_engine().begin() as conn:
        result = conn.execute(text(named_query), params)
        row = result.fetchone()
        return _to_dict(row) if row else None


def recent_metrics(limit: int = 120, host_id: str = "local") -> list[dict]:
    rows = fetch_all("SELECT * FROM metrics WHERE host_id=? ORDER BY ts DESC LIMIT ?", (host_id, limit))
    return list(reversed(rows))


def recent_events(limit: int = 50, host_id: str | None = None) -> list[dict]:
    if host_id:
        return fetch_all("SELECT * FROM events WHERE host_id=? ORDER BY ts DESC LIMIT ?", (host_id, limit))
    return fetch_all("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))


def open_anomalies(host_id: str = "local") -> list[dict]:
    return fetch_all(
        "SELECT * FROM anomalies WHERE host_id=? AND status IN ('open','diagnosed') ORDER BY ts DESC",
        (host_id,),
    )


def pending_recommendations(host_id: str = "local") -> list[dict]:
    return fetch_all(
        "SELECT * FROM recommendations WHERE host_id=? AND status='pending' ORDER BY ts DESC",
        (host_id,),
    )


# --------------------------------------------------------------- retenção --

def purge_old_data(metrics_cutoff_ts: float, events_cutoff_ts: float) -> dict:
    """
    Remove dados mais antigos que os cortes informados.

    Tabelas de alto volume (metrics, process_samples) usam o corte mais
    agressivo; tabelas de baixo volume e alto valor histórico (anomalies,
    recommendations, actions, events) usam um corte mais generoso. Só apaga
    registros já resolvidos/finalizados nas tabelas de estado, para nunca
    remover uma anomalia ou recomendação ainda em aberto.
    """
    with get_engine().begin() as conn:
        r_metrics = conn.execute(delete(metrics).where(metrics.c.ts < metrics_cutoff_ts))
        r_proc = conn.execute(delete(process_samples).where(process_samples.c.ts < metrics_cutoff_ts))
        r_anom = conn.execute(delete(anomalies).where(
            anomalies.c.ts < events_cutoff_ts, anomalies.c.status.in_(["resolved", "dismissed"]),
        ))
        r_rec = conn.execute(delete(recommendations).where(
            recommendations.c.ts < events_cutoff_ts,
            recommendations.c.status.in_(["executed", "dismissed", "failed"]),
        ))
        r_act = conn.execute(delete(actions).where(actions.c.ts < events_cutoff_ts))
        r_evt = conn.execute(delete(events).where(events.c.ts < events_cutoff_ts))
    return {
        "metrics": r_metrics.rowcount, "process_samples": r_proc.rowcount,
        "anomalies": r_anom.rowcount, "recommendations": r_rec.rowcount,
        "actions": r_act.rowcount, "events": r_evt.rowcount,
    }


def vacuum() -> None:
    """
    Recupera espaço em disco. SQLite: VACUUM de arquivo. PostgreSQL: VACUUM
    de tabela (não reduz o arquivo no disco imediatamente, mas marca espaço
    como reutilizável — é o comportamento normal do Postgres). Precisa rodar
    fora de uma transação em ambos os bancos.
    """
    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if engine.dialect.name == "postgresql":
            for table_name in _TABLES_BY_NAME:
                conn.execute(text(f"VACUUM {table_name}"))
        else:
            conn.execute(text("VACUUM"))


def storage_stats() -> dict:
    counts = {}
    for table_name in _TABLES_BY_NAME:
        row = fetch_one(f"SELECT COUNT(*) AS n FROM {table_name}")
        counts[table_name] = row["n"] if row else 0

    engine = get_engine()
    if engine.dialect.name == "postgresql":
        row = fetch_one("SELECT pg_database_size(current_database()) AS n")
        size_bytes = row["n"] if row else None
        location = str(engine.url).rsplit("@", 1)[-1]  # esconde credenciais, mostra só host/db
    else:
        size_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        location = str(DB_PATH)

    return {
        "row_counts": counts, "db_size_bytes": size_bytes,
        "backend": engine.dialect.name, "location": location,
    }
