"""
Motor de diagnóstico (seção 12) e de recomendações (seção 13).

Recebe as anomalias de métrica já pontuadas pelo AnomalyEngine, correlaciona
com os processos/rede da mesma amostra e produz um diagnóstico com evidências
e confiança — nunca só uma métrica isolada. Cada tipo de diagnóstico mapeia
para no máximo uma recomendação, seguindo a cadeia:

    Problema → Causa provável → Solução → Risco → Impacto → Ação
"""
from __future__ import annotations

import time


def diagnose(metric_anomalies: dict, scored_processes: list[dict], network: dict, disk_free_gb: float,
             ml_signal: dict | None = None) -> dict | None:
    """
    Retorna um dicionário de diagnóstico ou None se as anomalias de métrica
    não puderem ser explicadas por nenhuma regra de correlação (nesse caso
    ficam registradas como anomalia "aberta", sem causa identificada).

    `ml_signal`, quando presente, vem da camada multivariada (Isolation
    Forest) e tanto reforça a confiança das regras 1-4 quanto habilita a
    regra 5, para o caso em que nenhuma métrica isolada cruzou o z-score mas
    a combinação delas é estatisticamente incomum.
    """
    anomaly_procs = [p for p in scored_processes if p["is_anomaly"]]

    # --- regra 1: CPU (e opcionalmente rede) puxados por um processo -------
    if "cpu" in metric_anomalies and anomaly_procs:
        top = max(anomaly_procs, key=lambda p: p["cpu"])
        evidence = [
            f"CPU do processo {top['name']}: {top['cpu']:.0f}% (baseline: {top['baseline_cpu']:.1f}%)",
            f"CPU do sistema: {metric_anomalies['cpu']['value']:.0f}% "
            f"(esperado ~{metric_anomalies['cpu']['baseline_mean']:.0f}% ± "
            f"{metric_anomalies['cpu']['baseline_std']:.0f})",
        ]
        confidence = 0.6
        if "internet_latency_ms" in metric_anomalies:
            evidence.append("Aumento simultâneo de latência de rede")
            confidence += 0.15
        ratio = top["cpu"] / max(top["baseline_cpu"], 0.5)
        confidence += min(0.2, ratio / 100)
        if ml_signal:
            evidence.append("Modelo multivariado (Isolation Forest) também classifica a amostra como outlier")
            confidence += 0.1
        return {
            "type": "process_cpu_spike",
            "cause": f"Processo {top['name']}",
            "target": top["name"],
            "pid": top["pid"],
            "severity": metric_anomalies["cpu"]["severity"],
            "evidence": evidence,
            "confidence": round(min(confidence, 0.97), 2),
        }

    # --- regra 2: memória crescendo sem processo dominante -> possível leak
    if "ram" in metric_anomalies and not anomaly_procs:
        m = metric_anomalies["ram"]
        return {
            "type": "memory_growth",
            "cause": "Crescimento anômalo de memória sem processo dominante isolado",
            "target": "sistema",
            "pid": None,
            "severity": m["severity"],
            "evidence": [
                f"RAM em {m['value']:.0f}% (esperado ~{m['baseline_mean']:.0f}% ± {m['baseline_std']:.0f})",
                "Nenhum processo isolado ultrapassa o limiar de anomalia — "
                "consistente com um vazamento distribuído ou gradual",
            ],
            "confidence": 0.55,
        }

    # --- regra 3: disco quase cheio -----------------------------------
    if disk_free_gb is not None and disk_free_gb < 5:
        return {
            "type": "disk_low_space",
            "cause": "Armazenamento próximo da capacidade máxima",
            "target": "/",
            "pid": None,
            "severity": "Alta" if disk_free_gb < 2 else "Média",
            "evidence": [f"Espaço livre em disco: {disk_free_gb:.1f} GB"],
            "confidence": 0.9,
        }

    # --- regra 4: latência de internet alta mas rede local normal -----
    if "internet_latency_ms" in metric_anomalies:
        gw = network.get("gateway_latency_ms")
        dns = network.get("dns_latency_ms")
        local_ok = (gw is None or gw < 15) and (dns is None or dns < 30)
        if local_ok:
            m = metric_anomalies["internet_latency_ms"]
            return {
                "type": "external_network_latency",
                "cause": "Degradação de rede externa (fora da rede local)",
                "target": "internet",
                "pid": None,
                "severity": m["severity"],
                "evidence": [
                    f"Latência até a internet: {m['value']:.0f} ms (baseline ~{m['baseline_mean']:.0f} ms)",
                    f"Gateway: {gw} ms" if gw is not None else "Gateway indisponível para sonda",
                    f"DNS: {dns} ms" if dns is not None else "DNS indisponível para sonda",
                    "Rede local dentro do padrão — problema provavelmente está fora do host",
                ],
                "confidence": 0.7,
            }

    # --- regra 5: nenhuma métrica isolada cruzou o z-score, mas a combinação
    #     delas é estatisticamente incomum (visão puramente multivariada) ---
    if ml_signal and not metric_anomalies:
        feats = ml_signal["features"]
        evidence = [f"{k}: {v:.1f}" for k, v in feats.items()]
        evidence.append(
            "Nenhuma métrica isolada ultrapassou o limiar estatístico, mas a combinação "
            "das métricas é rara em relação ao comportamento histórico do host"
        )
        confidence = min(0.4 + ml_signal["anomaly_score"] * 0.3, 0.75)
        return {
            "type": "correlated_deviation",
            "cause": "Desvio correlacionado entre múltiplas métricas",
            "target": "sistema",
            "pid": None,
            "severity": "Média",
            "evidence": evidence,
            "confidence": round(confidence, 2),
        }

    return None


# ---------------------------------------------------------------- regras --
# diagnóstico -> receita de recomendação (ação, risco, impacto esperado)

_RECOMMENDATION_RULES = {
    "process_cpu_spike": {
        "action_type": "change_process_priority",
        "description": lambda d: f"Reduzir prioridade do processo {d['target']} (nice +5)",
        "risk": "LOW",
        "expected_impact": "Alto — libera CPU para os demais processos sem encerrar o processo",
    },
    "memory_growth": {
        "action_type": "flag_for_review",
        "description": lambda d: "Sinalizar sistema para revisão manual de possível vazamento de memória",
        "risk": "NONE",
        "expected_impact": "Nenhuma ação automática segura disponível — requer investigação humana",
    },
    "disk_low_space": {
        "action_type": "cleanup_temp_files",
        "description": lambda d: "Limpar arquivos temporários no diretório gerenciado",
        "risk": "LOW",
        "expected_impact": "Médio — libera espaço em disco imediatamente",
    },
    "external_network_latency": {
        "action_type": "run_network_diagnostic",
        "description": lambda d: "Executar diagnóstico de rede detalhado (gateway → DNS → internet)",
        "risk": "NONE",
        "expected_impact": "Informativo — não altera o sistema, apenas detalha a causa externa",
    },
    "correlated_deviation": {
        "action_type": "flag_for_review",
        "description": lambda d: "Sinalizar desvio correlacionado multivariado para revisão manual",
        "risk": "NONE",
        "expected_impact": "Nenhuma automação segura disponível para um desvio ainda sem causa isolada",
    },
}


def recommend(diagnosis: dict) -> dict | None:
    rule = _RECOMMENDATION_RULES.get(diagnosis["type"])
    if rule is None:
        return None
    return {
        "ts": time.time(),
        "action_type": rule["action_type"],
        "target": diagnosis["target"],
        "description": rule["description"](diagnosis),
        "risk": rule["risk"],
        "expected_impact": rule["expected_impact"],
        "status": "pending",
    }
