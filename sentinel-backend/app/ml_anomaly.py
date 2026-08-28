"""
Camada de detecção multivariada (seção 10 — "posteriormente, o Sentinel pode
incorporar modelos de aprendizado não supervisionado").

O z-score de `anomaly.py` olha uma métrica de cada vez. Isso deixa passar o
caso em que CPU, RAM e latência sobem *juntas* um pouco cada uma — nenhuma
isoladamente cruza o limiar, mas a combinação é rara. É exatamente o que o
Isolation Forest resolve: ele aprende a "forma" normal da nuvem de pontos
multidimensional e sinaliza pontos que são fáceis de isolar dela.

Deliberadamente NÃO substitui o z-score — os dois rodam em paralelo e o
diagnóstico usa a concordância entre eles como sinal de confiança extra.
"""
from __future__ import annotations

from collections import deque

from app.config import ML_CONTAMINATION, ML_MIN_SAMPLES, ML_RETRAIN_EVERY, ML_WINDOW

FEATURE_ORDER = ["cpu", "ram", "disk", "internet_latency_ms"]


class MultivariateAnomalyDetector:
    """
    Wrapper fino sobre sklearn.ensemble.IsolationForest.

    Importa sklearn dentro do construtor (não no topo do módulo) para que o
    resto do backend continue funcionando mesmo se scikit-learn não estiver
    instalado — a camada estatística sozinha já é um MVP completo, o ML é
    estritamente aditivo.
    """

    def __init__(self, window: int = ML_WINDOW, min_samples: int = ML_MIN_SAMPLES,
                 retrain_every: int = ML_RETRAIN_EVERY, contamination: float = ML_CONTAMINATION):
        self.window: deque[list[float]] = deque(maxlen=window)
        self.min_samples = min_samples
        self.retrain_every = retrain_every
        self.contamination = contamination
        self._model = None
        self._samples_since_fit = 0
        self._available = True
        try:
            import sklearn  # noqa: F401  (só testamos se o pacote existe; não usamos o módulo diretamente aqui)
        except ImportError:
            self._available = False

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    def _to_vector(self, values: dict[str, float]) -> list[float] | None:
        vec = [values.get(k) for k in FEATURE_ORDER]
        if any(v is None for v in vec):
            return None
        return vec

    def _fit(self) -> None:
        from sklearn.ensemble import IsolationForest
        self._model = IsolationForest(
            n_estimators=100,
            contamination=self.contamination,
            random_state=42,  # determinístico — importante para os testes e para não "flapejar" entre reinícios
        )
        self._model.fit(list(self.window))
        self._samples_since_fit = 0

    def update_and_score(self, values: dict[str, float]) -> dict | None:
        """
        Atualiza a janela de treino com a amostra atual e retorna um veredito
        para ELA MESMA (a amostra atual é avaliada contra o modelo treinado
        com as amostras anteriores — nunca contra um modelo que já a inclui,
        senão um outlier claro se "esconderia" ao ser absorvido no próprio
        treino).
        """
        if not self._available:
            return None
        vec = self._to_vector(values)
        if vec is None:
            return None

        result = None
        if self.is_trained:
            score = self._model.decision_function([vec])[0]   # >0 normal, <0 anômalo
            prediction = self._model.predict([vec])[0]          # 1 normal, -1 anômalo
            if prediction == -1:
                result = {
                    "is_anomaly": True,
                    "anomaly_score": round(-score, 4),  # positivo e maior = mais anômalo
                    "features": dict(zip(FEATURE_ORDER, vec)),
                }

        self.window.append(vec)
        self._samples_since_fit += 1

        needs_initial_fit = not self.is_trained and len(self.window) >= self.min_samples
        needs_retrain = self.is_trained and self._samples_since_fit >= self.retrain_every
        if needs_initial_fit or needs_retrain:
            self._fit()

        return result
