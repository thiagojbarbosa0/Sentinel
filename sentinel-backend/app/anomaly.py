"""
Motor de detecção de anomalias (seção 9 do projeto).

Duas camadas, ambas puramente estatísticas (sem ML nesta primeira versão,
como sugerido para o MVP):

1. Nível de métrica do sistema: janela deslizante + z-score. Cada host
   aprende sua própria média/desvio-padrão em vez de usar limites fixos —
   é o requisito central da seção 9 ("computador A" vs "computador B").

2. Nível de processo: baseline por EWMA (média móvel exponencial) por nome
   de processo. Um processo é candidato a "causa provável" quando está
   muito acima da própria baseline histórica, não de um limite genérico.
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque

from app.config import (
    EWMA_ALPHA,
    HISTORY_WINDOW,
    MIN_SAMPLES_FOR_BASELINE,
    ML_ENABLED,
    PROCESS_ANOMALY_MIN_CPU,
    PROCESS_ANOMALY_RATIO,
    Z_SCORE_THRESHOLD,
)
from app.ml_anomaly import MultivariateAnomalyDetector


class MetricBaseline:
    """Janela deslizante de uma métrica escalar (ex: cpu, ram, latência)."""

    def __init__(self, window: int = HISTORY_WINDOW):
        self.window: deque[float] = deque(maxlen=window)

    def push(self, value: float) -> None:
        self.window.append(value)

    def zscore(self, value: float) -> tuple[float, float, float] | None:
        """Retorna (z, mean, std) ou None se ainda não há baseline suficiente."""
        if len(self.window) < MIN_SAMPLES_FOR_BASELINE:
            return None
        mean = statistics.fmean(self.window)
        std = statistics.pstdev(self.window) or 1e-6
        z = (value - mean) / std
        return z, mean, std


class ProcessBaseline:
    """EWMA por nome de processo — sobrevive a PIDs que mudam entre execuções."""

    def __init__(self, alpha: float = EWMA_ALPHA):
        self.alpha = alpha
        self._ewma: dict[str, float] = {}
        self._seen: dict[str, int] = defaultdict(int)

    def update_and_score(self, name: str, cpu: float) -> tuple[float, bool]:
        """
        Atualiza a baseline do processo com o valor atual e retorna
        (baseline_antes_da_atualizacao, is_anomaly).
        A atualização usa o mínimo entre o valor atual e um teto, para que um
        pico real não "puxe" a própria baseline para cima antes de ser
        classificado — evita que o motor aprenda o pico como normal.
        """
        baseline = self._ewma.get(name, cpu)
        self._seen[name] += 1

        is_anomaly = (
            self._seen[name] > 5
            and cpu > baseline * PROCESS_ANOMALY_RATIO
            and cpu > PROCESS_ANOMALY_MIN_CPU
        )

        update_value = min(cpu, baseline * 2 + 1) if is_anomaly else cpu
        self._ewma[name] = self.alpha * update_value + (1 - self.alpha) * baseline
        return round(baseline, 2), is_anomaly


class AnomalyEngine:
    def __init__(self):
        self.metric_baselines: dict[str, MetricBaseline] = {
            "cpu": MetricBaseline(),
            "ram": MetricBaseline(),
            "disk": MetricBaseline(),
            "internet_latency_ms": MetricBaseline(),
        }
        self.process_baseline = ProcessBaseline()
        self.ml_detector = MultivariateAnomalyDetector() if ML_ENABLED else None

    def score_metrics(self, values: dict[str, float]) -> dict[str, dict]:
        """
        Atualiza baselines e retorna anomalias de métricas do sistema.
        `values` deve conter as mesmas chaves de `self.metric_baselines`.
        """
        results = {}
        for key, baseline in self.metric_baselines.items():
            value = values.get(key)
            if value is None:
                continue
            scored = baseline.zscore(value)
            baseline.push(value)
            if scored is None:
                continue
            z, mean, std = scored
            if abs(z) >= Z_SCORE_THRESHOLD:
                results[key] = {
                    "value": value,
                    "baseline_mean": round(mean, 2),
                    "baseline_std": round(std, 2),
                    "zscore": round(z, 2),
                    "severity": "Alta" if abs(z) >= Z_SCORE_THRESHOLD * 1.6 else "Média",
                }
        return results

    def score_processes(self, processes: list[dict]) -> list[dict]:
        """Anexa baseline/is_anomaly a cada processo da amostra atual."""
        scored = []
        for p in processes:
            baseline, is_anomaly = self.process_baseline.update_and_score(p["name"], p["cpu"])
            scored.append({**p, "baseline_cpu": baseline, "is_anomaly": is_anomaly})
        return scored

    def score_multivariate(self, values: dict[str, float]) -> dict | None:
        """
        Camada de ML (seção 10): olha CPU/RAM/disco/latência juntos e pega
        desvios correlacionados sutis que nenhuma métrica isolada cruzaria o
        z-score sozinha. Retorna None enquanto o modelo ainda não tem
        amostras suficientes para treinar, ou se scikit-learn não está
        instalado (a camada estatística funciona de forma totalmente
        independente disso).
        """
        if self.ml_detector is None:
            return None
        return self.ml_detector.update_and_score(values)


def compute_health_score(cpu: float, ram: float, disk: float, internet_latency: float | None) -> float:
    """
    Score composto 0-100 (seção 17). Pesos refletem o quanto cada dimensão
    costuma indicar um problema perceptível pelo usuário: CPU e latência
    pesam mais que disco, que degrada de forma mais gradual.
    """
    latency = internet_latency or 20.0
    penalty = (
        max(0.0, cpu - 40) * 0.35
        + max(0.0, ram - 60) * 0.35
        + max(0.0, disk - 70) * 0.25
        + max(0.0, latency - 30) * 0.30
    )
    return round(max(5.0, min(100.0, 100.0 - penalty)), 1)
