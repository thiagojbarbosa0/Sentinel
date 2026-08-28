"""
Motor de automação (seções 14 e 15).

Cada ação é uma função pura de efeito bem delimitado, com:
  - nível de risco declarado,
  - possibilidade de rollback (quando aplicável),
  - execução restrita a um escopo seguro (ex: só apaga arquivos dentro de
    MANAGED_TEMP_DIR, nunca em diretórios arbitrários do sistema).

Isso é o que a seção 15 chama de evitar que o sistema "se torne um agente
com acesso irrestrito ao computador".
"""
from __future__ import annotations

import time
from typing import Callable

import psutil

from app.config import MANAGED_TEMP_DIR

ActionResult = dict  # {"status": "executed"|"failed", "before":..., "after":..., "rollback":..., "error":...}

MANAGED_TEMP_DIR.mkdir(parents=True, exist_ok=True)


def action_change_process_priority(target_name: str, delta: int = 5) -> ActionResult:
    """
    Reduz a prioridade (aumenta o 'nice') de todos os processos com o nome
    alvo. Aumentar niceness é permitido a processos não-root no Linux, então
    a ação funciona sem privilégios elevados — e é reversível.
    """
    before, after, touched = [], [], []
    try:
        for p in psutil.process_iter(attrs=["pid", "name", "nice"]):
            if p.info["name"] != target_name:
                continue
            proc = psutil.Process(p.info["pid"])
            old_nice = proc.nice()
            before.append({"pid": proc.pid, "nice": old_nice})
            new_nice = min(old_nice + delta, 19)  # 19 = menor prioridade possível no Linux
            proc.nice(new_nice)
            after.append({"pid": proc.pid, "nice": proc.nice()})
            touched.append(proc.pid)
        if not touched:
            return {"status": "failed", "before": before, "after": after, "rollback": None,
                     "error": f"Nenhum processo em execução chamado '{target_name}'"}
        return {"status": "executed", "before": before, "after": after,
                 "rollback": {"action": "change_process_priority", "pids": touched, "restore": before}, "error": None}
    except psutil.AccessDenied as e:
        return {"status": "failed", "before": before, "after": after, "rollback": None, "error": str(e)}


def rollback_change_process_priority(rollback_data: dict) -> ActionResult:
    restored = []
    for entry in rollback_data.get("restore", []):
        try:
            proc = psutil.Process(entry["pid"])
            proc.nice(entry["nice"])
            restored.append({"pid": entry["pid"], "nice": proc.nice()})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"status": "executed", "before": None, "after": restored, "rollback": None, "error": None}


def action_cleanup_temp_files(max_age_hours: float = 0) -> ActionResult:
    """
    Apaga arquivos dentro do diretório GERENCIADO pelo Sentinel (nunca fora
    dele). Em um ambiente real esse escopo seria configurado explicitamente
    pelo usuário (ex: pasta de cache de um app específico).
    """
    before_files = list(MANAGED_TEMP_DIR.glob("*"))
    before_size = sum(f.stat().st_size for f in before_files if f.is_file())
    cutoff = time.time() - max_age_hours * 3600
    removed = []
    try:
        for f in before_files:
            if f.is_file() and f.stat().st_mtime <= cutoff:
                size = f.stat().st_size
                f.unlink()
                removed.append({"file": f.name, "size_bytes": size})
        freed = sum(r["size_bytes"] for r in removed)
        return {
            "status": "executed",
            "before": {"file_count": len(before_files), "total_bytes": before_size},
            "after": {"removed_count": len(removed), "freed_bytes": freed},
            "rollback": None,  # exclusão de arquivo não é reversível — declarado explicitamente
            "error": None,
        }
    except OSError as e:
        return {"status": "failed", "before": {"file_count": len(before_files)}, "after": None,
                 "rollback": None, "error": str(e)}


def action_run_network_diagnostic() -> ActionResult:
    """Ação puramente informativa — não modifica nada no host."""
    from app.collectors import collect_network_latency
    result = collect_network_latency()
    return {"status": "executed", "before": None, "after": result, "rollback": None, "error": None}


def action_flag_for_review(target: str) -> ActionResult:
    """Sem automação segura disponível: apenas registra o sinalizador."""
    return {"status": "executed", "before": None,
             "after": {"flagged": target, "note": "Requer investigação manual"}, "rollback": None, "error": None}


_ACTIONS: dict[str, Callable[..., ActionResult]] = {
    "change_process_priority": action_change_process_priority,
    "cleanup_temp_files": action_cleanup_temp_files,
    "run_network_diagnostic": action_run_network_diagnostic,
    "flag_for_review": action_flag_for_review,
}


def execute_action(action_type: str, target: str) -> ActionResult:
    fn = _ACTIONS.get(action_type)
    if fn is None:
        return {"status": "failed", "before": None, "after": None, "rollback": None,
                 "error": f"Ação desconhecida: {action_type}"}
    if action_type == "change_process_priority":
        return fn(target)
    if action_type == "flag_for_review":
        return fn(target)
    return fn()


def rollback_action(action_type: str, rollback_data: dict) -> ActionResult:
    if action_type == "change_process_priority":
        return rollback_change_process_priority(rollback_data)
    return {"status": "failed", "before": None, "after": None, "rollback": None,
             "error": f"Ação '{action_type}' não é reversível"}
