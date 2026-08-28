# Sentinel — Backend (MVP)

Backend real (não simulado) do projeto Sentinel: coleta métricas de verdade
do host com `psutil`, aprende o comportamento normal do sistema, detecta
anomalias estatisticamente, diagnostica causas prováveis e pode executar
ações de otimização com segurança e rollback.

Implementa o MVP descrito na seção 23 do documento original, itens 1–10.

## Como rodar

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

A API sobe em `http://localhost:8000`. Documentação interativa (Swagger)
automática em `http://localhost:8000/docs`.

Funciona melhor em Linux (é onde o MVP foca — seção 23, item 1). Em outros
SOs, `load_avg` e a leitura de gateway via `/proc/net/route` ficam
indisponíveis, mas o resto (CPU, RAM, disco, processos, latência de
internet) continua funcionando.

## O que ele realmente faz

Nada aqui é mockado. O agente embutido roda em loop assíncrono
(`app/scheduler.py`) a cada `SENTINEL_INTERVAL` segundos (padrão 2s) e:

1. **Coleta** (`app/collectors.py`) — CPU, RAM, disco, I/O, top processos por
   CPU, e uma escada de latência gateway → DNS → internet via sondas TCP
   (não usa ICMP, então não precisa de root).
2. **Detecta anomalias** (`app/anomaly.py` + `app/ml_anomaly.py`) — duas
   camadas estatísticas independentes, que se reforçam mas não dependem uma
   da outra:
   - **Métrica do sistema (univariada)**: janela deslizante + **z-score**.
     Cada host aprende sua própria média/desvio-padrão, então o que é
     anômalo no seu notebook não é necessariamente anômalo em um servidor.
   - **Processo**: baseline por **EWMA** (média móvel exponencial) por nome
     de processo. Um processo dispara alerta quando foge muito da própria
     história, não de um limite fixo.
   - **Multivariada (ML)**: `Isolation Forest` (scikit-learn) treinado sobre
     o vetor `[cpu, ram, disk, latência]` das últimas amostras. Pega o caso
     que o z-score sozinho não vê: CPU, RAM e latência subindo um pouco
     *cada uma*, sem nenhuma cruzar o limiar individualmente, mas juntas
     formando um ponto raro no espaço multidimensional (seção 10). Se
     scikit-learn não estiver instalado, essa camada simplesmente não roda —
     o resto do backend funciona normalmente sem ela.
3. **Diagnostica e correlaciona** (`app/diagnostics.py`) — junta a anomalia
   de métrica com o processo/rede da mesma amostra em vez de gerar alertas
   soltos, e sempre expõe as evidências que sustentam a conclusão + um score
   de confiança. Quando o Isolation Forest concorda com uma regra estatística,
   a confiança sobe; quando ele sozinho pega um desvio correlacionado que
   nenhuma métrica isolada explicaria, isso vira um diagnóstico próprio
   (`correlated_deviation`) — mais uma prova de conceito do que uma automação
   segura, já que não há "causa isolada" para agir em cima.
4. **Recomenda** (`app/diagnostics.py`) — cada tipo de diagnóstico mapeia
   para no máximo uma ação, com risco e impacto esperado declarados.
5. **Automatiza com segurança** (`app/automation.py`) — as ações realmente
   executam no host, mas dentro de um escopo restrito:
   - `change_process_priority`: aumenta o *nice* do processo (funciona sem
     root no Linux) — **reversível**, o valor original é salvo para rollback.
   - `cleanup_temp_files`: só apaga arquivos dentro de um diretório
     "gerenciado" pelo Sentinel (`managed_tmp/`), nunca em um caminho
     arbitrário do sistema.
   - `run_network_diagnostic`: ação somente leitura.
   - `flag_for_review`: quando não existe automação segura, o sistema
     admite isso e pede revisão humana em vez de agir às cegas.
6. **Persiste tudo** (`app/database.py`) — SQLite por padrão (`sentinel.db`),
   com suporte real a PostgreSQL/TimescaleDB via `SENTINEL_DB_URL` (ver seção
   abaixo) — schema pensado como série temporal *append-only* desde o
   início, então trocar de banco não muda o resto do código. Uma rotina de
   **retenção** (`app/scheduler.retention_loop`) roda em paralelo e apaga
   métricas antigas automaticamente — sem isso o banco cresceria pra
   sempre. Anomalias e recomendações em aberto nunca são apagadas,
   independentemente da idade.

## Multi-dispositivo (seção 22)

O backend monitora nativamente a máquina onde roda (host `local`), mas
também aceita dados de **agentes remotos** rodando em outras máquinas —
cada host com seu próprio motor de baseline/anomalia, para que os dados de
um não vazem no aprendizado do outro.

**No host que você quer monitorar remotamente:**
```bash
pip install requests psutil
SENTINEL_SERVER_URL=http://ip-do-backend:8000 \
SENTINEL_API_KEY=mesma-chave-do-backend \
SENTINEL_HOST_NAME=notebook-ana \
python agent.py
```

`agent.py` é um script standalone — não sobe FastAPI/uvicorn, só coleta com
`app/collectors.py` e envia via `POST /ingest` no intervalo configurado.

**No backend central**, os dados aparecem automaticamente:
```bash
curl http://localhost:8000/hosts                                  # inventário de hosts
curl "http://localhost:8000/snapshot?host_id=notebook-ana"         # estado daquele host
curl "http://localhost:8000/metrics/history?host_id=notebook-ana"  # histórico isolado
```

**Limite intencional:** ações de automação (renice, limpeza de arquivo) só
executam de verdade no host `local` — mesmo em N4. Aprovar uma recomendação
de um host remoto retorna `422`, porque o backend não tem como controlar um
processo numa máquina onde ele não está rodando; isso exigiria um agente de
*execução* (não só de coleta) rodando lá, que é um passo além do que
este MVP cobre (ver "Limitações conhecidas" abaixo).

## PostgreSQL / TimescaleDB (seção 19)

O backend usa SQLite por padrão (zero configuração), mas a camada de
armazenamento (`app/database.py`) é escrita em **SQLAlchemy Core** — a mesma
lógica funciona contra PostgreSQL só trocando a variável de ambiente:

```bash
pip install -r requirements-postgres.txt   # adiciona o driver psycopg2
SENTINEL_DB_URL="postgresql+psycopg2://usuario:senha@host:5432/sentinel" python main.py
```

Isso não é só documentado — a suíte de testes inteira (53 testes) passa sem
nenhuma alteração contra um PostgreSQL real:
```bash
pip install -r requirements-postgres.txt
SENTINEL_DB_URL="postgresql+psycopg2://usuario:senha@host:5432/sentinel" pytest tests/ -v
```
e o backend foi validado de ponta a ponta contra um Postgres de verdade
durante o desenvolvimento: subiu, coletou métricas, gravou recomendações, e
a purga de retenção rodou `VACUUM` por tabela — tudo confirmado consultando
o banco diretamente via `psql`, não só através do próprio código do app.

Como o TimescaleDB é uma extensão do PostgreSQL (não um banco diferente), um
Postgres configurado aqui já pode virar TimescaleDB depois — rodar
`SELECT create_hypertable('metrics', 'ts')` etc. — sem tocar em
`app/database.py` de novo.

## Docker

```bash
docker compose up --build
```

Monitorar o host a partir de um container exige abrir mão de parte do
isolamento do Docker — não tem como contornar, é a mesma troca que qualquer
agente de observabilidade faz. O `docker-compose.yml` já vem configurado
com `pid: host`, `network_mode: host` e o disco do host montado read-only
em `/hostfs` — os comentários no arquivo explicam por que cada um é
necessário. Se você só quer o backend central recebendo dados de agentes
remotos (sem monitorar a própria máquina do container), pode remover essas
três linhas sem problema.

> **Nota:** não foi possível testar o build da imagem neste ambiente (sem
> Docker disponível na sandbox onde este projeto foi construído). O
> Dockerfile foi revisado manualmente e as dependências já são as mesmas
> testadas via `pip install -r requirements.txt` — mas vale um `docker
> compose up --build` de verdade antes de confiar em produção.

## Endpoints principais

| Método | Rota | Descrição |
|---|---|---|
| GET | `/health` | Verificação simples de que o processo está de pé (uptime do backend) |
| GET | `/hosts` | Inventário de hosts conhecidos (local + agentes remotos) |
| GET | `/snapshot?host_id=` | Estado atual completo de um host (métricas, processos, diagnóstico ativo) |
| POST | `/ingest` | Recebe uma amostra de um agente remoto (usado por `agent.py`) |
| GET | `/metrics/history?limit=&host_id=` | Série histórica de métricas do sistema |
| GET | `/processes` | Top processos da última amostra, com baseline e flag de anomalia |
| GET | `/processes/history?name=` | Histórico de CPU/RAM de um processo específico |
| GET | `/network` | Última leitura de latência gateway/DNS/internet |
| GET | `/anomalies?status=` | Anomalias detectadas (open / diagnosed / resolved / dismissed) |
| GET | `/recommendations?status=` | Recomendações geradas |
| POST | `/recommendations/{id}/approve` | Executa a ação recomendada (nível 3 de autonomia) |
| POST | `/recommendations/{id}/dismiss` | Descarta a recomendação |
| GET | `/actions` | Log de ações executadas, com before/after/rollback |
| GET/POST | `/autonomy` | Consulta/altera o nível de autonomia (1–4, seção 14) |
| GET | `/events` | Linha do tempo de eventos do sistema |
| GET | `/storage/stats` | Contagem de linhas por tabela e tamanho do banco (SQLite ou PostgreSQL) |
| POST | `/storage/purge` | Dispara a retenção manualmente (fora do ciclo periódico) |
| WS | `/ws/live` | Stream do snapshot atual a cada ~1s, sem polling |

Todas as respostas são JSON puro — o frontend/dashboard (protótipo já
construído) pode consumir via `fetch`/`WebSocket` diretamente, sem nenhuma
adaptação de formato.

## Níveis de autonomia (seção 14)

- **N1 — Observação**: só coleta e persiste, não gera recomendação.
- **N2 — Recomendação**: gera recomendação, mas não executa nada sozinho.
- **N3 — Aprovação** *(padrão)*: gera recomendação e aguarda `POST
  /recommendations/{id}/approve`.
- **N4 — Automação**: ações de risco `NONE`/`LOW` são executadas
  automaticamente pelo `scheduler.py`, sempre com log e (quando aplicável)
  rollback salvo.

## Testes

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

53 testes cobrindo (rodam contra SQLite por padrão; contra PostgreSQL real
com `SENTINEL_DB_URL` definida, sem nenhuma alteração no código de teste):
- `test_multihost.py` — dois hosts com o mesmo valor de CPU recebem
  diagnósticos diferentes (cada um com seu baseline), `/ingest` rejeita o
  host_id `local` (reservado), e aprovar uma recomendação de host remoto
  retorna 422 em vez de tentar (e falhar silenciosamente) controlar um
  processo que não está nesta máquina.
- `test_anomaly.py` — z-score respeita o baseline de cada host (reproduz o
  exemplo da seção 9: mesmo valor de CPU é anômalo num host e normal em
  outro), EWMA por processo não "aprende" o próprio pico como normal.
- `test_ml_anomaly.py` — Isolation Forest fica quieto com dados de treino
  normais, sinaliza um desvio correlacionado que nenhuma métrica isolada
  cruzaria sozinha, e re-treina no intervalo configurado.
- `test_diagnostics.py` — cada regra de correlação isoladamente, incluindo
  os casos em que o motor corretamente **não** encontra causa provável, e a
  regra multivariada isolada (`correlated_deviation`) só dispara quando
  nenhuma regra estatística já explicou o desvio.
- `test_automation.py` — ações executam de verdade (inclusive `renice` num
  processo real de teste + rollback), e a limpeza de arquivos nunca sai do
  diretório gerenciado mesmo quando testada contra um caminho fora dele.
- `test_retention.py` — purga remove dados antigos mas nunca uma anomalia
  ou recomendação ainda em aberto, independentemente da idade.
- `test_api.py` — fluxo completo aprovar/descartar recomendação, 404/409 em
  casos de borda, persistência do nível de autonomia, e a chave de API
  bloqueando rotas de escrita sem afetar leitura.

Os testes desligam o loop de coleta em background (`SENTINEL_DISABLE_SCHEDULER=1`,
setado automaticamente por `tests/conftest.py`) para não rodar contra o host
de CI/dev de forma não-determinística, e usam um banco SQLite temporário
isolado por execução.

## Segurança da API

Sem `SENTINEL_API_KEY` definida, a API funciona livremente — adequado para
uso local. Ao definir a variável, toda rota que **altera o host** (aprovar
recomendação, mudar nível de autonomia) passa a exigir o header `X-API-Key`;
rotas de leitura continuam abertas.

```bash
SENTINEL_API_KEY=uma-chave-forte python main.py
curl -X POST http://localhost:8000/autonomy \
     -H "X-API-Key: uma-chave-forte" -H "Content-Type: application/json" \
     -d '{"level": 4}'
```

## Configurações persistentes

O nível de autonomia sobrevive a reinícios do processo — é salvo numa
tabela `settings` no banco configurado (SQLite ou PostgreSQL, via
`app/database.get_setting`/`set_setting`) toda vez que é alterado via
`POST /autonomy`, e recarregado no `startup` da API.

## Limitações conhecidas deste MVP

- `cleanup_temp_files` só atua em `managed_tmp/`; para virar útil de
  verdade, o usuário precisa apontar `SENTINEL_TEMP_DIR` para uma pasta de
  cache real (ex: cache de um app específico) — decisão intencional para não
  dar ao Sentinel acesso irrestrito ao disco (seção 15).
- O Isolation Forest treina só com as métricas do próprio host desde que o
  processo subiu — não tem "memória" entre reinícios (a janela de treino
  vive em memória, não no banco). Isso é aceitável para o MVP, mas o próximo
  passo natural seria persistir/recarregar a janela de features.
- Multi-dispositivo cobre **coleta e diagnóstico** remotos, não **execução**
  remota: aprovar uma recomendação de um host que não é o `local` retorna
  422 de propósito, porque executar a ação exigiria um agente de execução
  (não só de coleta) rodando naquela máquina — fora do escopo desta versão.
- Detecção de gateway (macOS/Windows) depende de parsear a saída de
  `route`/`ipconfig` via subprocess — funciona nos formatos padrão desses
  comandos, mas é mais frágil que a leitura direta de `/proc/net/route` no
  Linux.
- O Dockerfile/compose foram revisados manualmente mas não têm um `docker
  build` real testado (sem Docker disponível no ambiente onde este projeto
  foi construído) — vale validar antes de depender disso em produção.

## Variáveis de ambiente (opcionais)

| Variável | Padrão | Descrição |
|---|---|---|
| `SENTINEL_INTERVAL` | `2.0` | Segundos entre cada ciclo de coleta |
| `SENTINEL_WINDOW` | `120` | Amostras mantidas na janela de baseline |
| `SENTINEL_TOP_N` | `12` | Quantidade de processos monitorados em detalhe a cada ciclo |
| `SENTINEL_AUTONOMY` | `3` | Nível de autonomia inicial (1–4), só usado se nada foi persistido ainda |
| `SENTINEL_DB` | `sentinel.db` | Caminho do arquivo SQLite (ignorado se `SENTINEL_DB_URL` estiver definida) |
| `SENTINEL_DB_URL` | *(vazio → SQLite)* | URL completa do banco; defina para usar PostgreSQL (`postgresql+psycopg2://...`) |
| `SENTINEL_TEMP_DIR` | `managed_tmp/` | Diretório onde `cleanup_temp_files` tem permissão de apagar |
| `SENTINEL_API_KEY` | *(vazio)* | Se definida, exige `X-API-Key` nas rotas que alteram o host |
| `SENTINEL_DISABLE_SCHEDULER` | `0` | Usado pelos testes para subir a API sem o loop de coleta |
| `SENTINEL_RETENTION_DAYS_METRICS` | `14` | Métricas/amostras de processo mais antigas que isso são apagadas |
| `SENTINEL_RETENTION_DAYS_EVENTS` | `90` | Anomalias/recomendações/ações/eventos resolvidos mais antigos que isso são apagados |
| `SENTINEL_RETENTION_INTERVAL` | `3600` | Segundos entre cada ciclo de retenção automática |
| `SENTINEL_ML_ENABLED` | `1` | Liga/desliga a camada de detecção multivariada (Isolation Forest) |
| `SENTINEL_ML_MIN_SAMPLES` | `40` | Amostras mínimas antes do primeiro treino do modelo |
| `SENTINEL_ML_RETRAIN_EVERY` | `20` | Re-treina o modelo a cada N amostras novas |
| `SENTINEL_ML_WINDOW` | `300` | Amostras mantidas na janela de treino |
| `SENTINEL_ML_CONTAMINATION` | `0.05` | Fração esperada de outliers (parâmetro do Isolation Forest) |
| `SENTINEL_DISK_PATH` | `/` | Caminho medido para uso de disco — aponte para `/hostfs` ao rodar em Docker |
| `SENTINEL_HOST_NAME` | hostname da máquina | Nome de exibição do host local no inventário `/hosts` |

**Só para `agent.py` (o script standalone, não o backend):**

| Variável | Padrão | Descrição |
|---|---|---|
| `SENTINEL_SERVER_URL` | *(obrigatório)* | URL do backend central para onde enviar os dados |
| `SENTINEL_API_KEY` | *(vazio)* | Mesma chave configurada no backend, se houver |
| `SENTINEL_HOST_NAME` | hostname da máquina | Identificador deste host no backend central |
| `SENTINEL_INTERVAL` | `5` | Segundos entre cada envio |
