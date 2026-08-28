"""
Configuração compartilhada dos testes.

As variáveis de ambiente precisam ser definidas ANTES de qualquer módulo de
`app` ser importado, porque `app/config.py` as lê no import (não em tempo de
execução). O conftest.py do pytest é carregado antes dos arquivos de teste
da mesma pasta, então este é o lugar certo para isso.
"""
import os
import tempfile

_tmp_root = tempfile.mkdtemp(prefix="sentinel-tests-")
os.environ["SENTINEL_DISABLE_SCHEDULER"] = "1"
os.environ["SENTINEL_DB"] = os.path.join(_tmp_root, "test.db")
os.environ["SENTINEL_TEMP_DIR"] = os.path.join(_tmp_root, "managed_tmp")
os.environ.setdefault("SENTINEL_API_KEY", "")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    """Cada teste começa com um schema vazio, recriado do zero."""
    from app import database as db
    db.reset_db()
    yield
