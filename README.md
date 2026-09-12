# queue-totem-core

Fila de atendimento presencial (senha de totem, painel de chamada, gestão pela recepção) como um
**router FastAPI plugável**, agnóstico de domínio.

Contexto completo de design e decisões: [`docs/PLANO.md`](docs/PLANO.md).

## Instalação

Sem PyPI — dependência git fixada por tag semver:

```
queue-totem-core @ git+https://github.com/<usuario>/queue-totem-core.git@v0.4.0
```

Requisitos do host: Python 3.11+, FastAPI 0.104+, SQLAlchemy 2.0 async, Pydantic v2.

## Uso

```python
from fastapi import FastAPI
from queue_totem_core import (
    QueueConfig,
    TicketTypeConfig,
    build_queue_router,
    init_queue_tables,
)

config = QueueConfig(
    ticket_types=[
        TicketTypeConfig(code="AG", label="Agendamento", priority_source="external"),
        TicketTypeConfig(code="TR", label="Triagem", priority_source="self_declared"),
        TicketTypeConfig(code="FM", label="Farmácia", priority_source="none"),
    ],
    priority_order=[("AG", True), ("AG", False), ("TR", True), ("TR", False), ("FM", False)],
    daily_reset=True,
    max_recall_attempts=1,
    timezone="America/Fortaleza",  # define o "dia" da fila; importante em container UTC
    normals_per_priority=2,        # 1 prioritário a cada 2 normais (0 = prioridade estrita)
)

app = FastAPI()
app.include_router(
    build_queue_router(
        get_db=get_db,                        # dependency do host: AsyncSession
        config=config,
        manage_dependency=require_role("admin", "recepcionista"),
        read_dependency=get_current_user,
    ),
    prefix="/queue",
    tags=["queue"],
)


@app.on_event("startup")  # ou no lifespan
async def startup():
    await init_queue_tables(engine)  # bancos novos/dev; em produção use as migrations
```

## Filas por estação (v0.3.0)

Por padrão o pacote opera em **estação única** — uma fila só, exatamente como na v0.2.0.
Declarando `stations`, cada ponto de atendimento passa a ter fila e "chamar próximo" próprios:

```python
from queue_totem_core import StationConfig

config = QueueConfig(
    ticket_types=[...],
    priority_order=[...],
    stations=[
        StationConfig(code="recepcao", label="Recepção"),
        StationConfig(code="triagem", label="Triagem"),
        StationConfig(code="consultorio", label="Consultório"),
    ],
    entry_station="recepcao",  # onde a senha nasce ao ser emitida no totem
)
```

- `station` é **string opaca**, como `reference_code`: o pacote não sabe o que cada uma
  significa nem em que ordem vêm. **Roteamento é do host**, via `PATCH /tickets/{id}/station`.
- A senha nasce em `entry_station` e caminha por `PATCH /tickets/{id}/station`, que a recoloca
  em `na_fila` na estação de destino. A posição não é o fim da fila: a ordem usa `queued_since`,
  o carimbo de **chegada original**, preservado ao longo de toda a jornada. Quem esperou 40
  minutos na recepção não recomeça atrás de quem acabou de chegar.
- O PATCH responde com `previous_station` e `previous_station_seconds` (tempo gasto na etapa
  anterior). O pacote não guarda histórico de etapas — quem quiser auditar a jornada persiste
  esses campos no próprio domínio.
- `priority_order` e `normals_per_priority` continuam valendo, agora **dentro de cada estação**.
- Uma estação pode ter **vários postos atendendo em paralelo** (dois consultórios, dois guichês).
  `POST /tickets/next?station=X&room=Y` grava a sala da chamada, e o painel a devolve junto com o
  chamado. `room` é string opaca, opcional, e pertence à **chamada**: rechamar mantém, chamar de
  novo substitui, e mudar de estação limpa — anunciar o posto da etapa anterior mandaria a pessoa
  para o lugar errado.
- Sem `stations`, `POST /tickets/next?station=...`, `GET /tickets?station=...` e o PATCH de
  estação respondem **400**.

## Migrations

O schema do pacote é versionado pelo próprio pacote, numa cadeia Alembic separada da do host:
a tabela de versões é **`queue_versions_table`**, nunca `alembic_version`.

```bash
python -m queue_totem_core.migrate upgrade head --url postgresql+asyncpg://user:senha@host/db
```

A URL também pode vir de `QUEUE_TOTEM_DATABASE_URL` ou `DATABASE_URL`. Drivers sync e async
são aceitos. Outros comandos: `current`, `history`, `heads`, `stamp`, `downgrade <rev>`.

- **Rode no deploy, em container avulso, ao lado do `alembic upgrade head` do host** — nunca no
  boot da aplicação: o `lifespan` não deve ter poder de alterar schema.
- **Bancos que já rodavam o pacote** (tabela criada por `init_queue_tables`, sem controle de
  versão) são detectados: a revisão base faz apenas o *stamp*, sem recriar a tabela, e só o
  delta é aplicado. Num banco vazio, cria tudo.
- `init_queue_tables` continua existindo para bancos novos e desenvolvimento, mas **não é
  mecanismo de evolução**: `create_all` não altera tabela existente.

## Endpoints

| Verbo | Path | Acesso | Função |
|---|---|---|---|
| POST | `/tickets` | Público | Totem emite senha (número sequencial do dia por tipo) |
| GET | `/display` | Público | Painel de TV: chamada atual + últimas N; com estações, um corrente por estação |
| GET | `/tickets` | `read_dependency` | Fila do dia, ordenada por prioridade + FIFO (`?status=`, `?station=` opcionais) |
| POST | `/tickets/next` | `manage_dependency` | Chamar próxima senha (`?station=` e `?room=` opcionais; aplica `priority_order` + `normals_per_priority`) |
| PATCH | `/tickets/{id}/recall` | `manage_dependency` | Chamar novamente (respeita `max_recall_attempts`) |
| PATCH | `/tickets/{id}/no-show` | `manage_dependency` | Marcar não compareceu |
| PATCH | `/tickets/{id}/status` | `manage_dependency` | Transição manual: `em_atendimento` / `concluido` |
| PATCH | `/tickets/{id}/station` | `manage_dependency` | Mover para outra estação (volta a `na_fila`) — 400 em modo estação única |

## Política de prioridade ("chamar próximo")

`priority_order` define o ranking de `(tipo, is_priority)` e o desempate é FIFO por chegada
original (`queued_since`, com fallback para `created_at` em senhas anteriores à v0.3.0). Como isso combina prioritários e normais depende de `normals_per_priority`:

- **`0` (padrão) — prioridade estrita**: esvazia todos os prioritários antes dos
  normais. Simples, mas uma fila cheia de prioritários trava os normais.
- **`N > 0` — intercalação justa**: a cada `N` senhas normais chamadas em sequência,
  a próxima chamada é de um prioritário aguardando. Ex.: `N=2` produz o padrão
  `normal, normal, prioritário, normal, normal, prioritário, …`. Se só há um dos
  grupos aguardando, chama esse; prioritários nunca "furam" mais do que a cota.

## Ciclo de vida

```
na_fila → chamado → em_atendimento → concluido
                  ↘ nao_compareceu (após esgotar max_recall_attempts)
```

## Pontos de atenção

- **Referência a entidades do host**: apenas `reference_code`/`reference_label` (strings opacas).
  Validar semântica de domínio (ex.: "protocolo existe?") é responsabilidade do host, *antes* de
  chamar a emissão.
- **Persistência**: o pacote tem `Base`/metadata próprios e a tabela `queue_tickets`. Não
  integrar à cadeia Alembic do host — o pacote tem a sua, com `version_table` próprio
  (ver [Migrations](#migrations)).
- **Concorrência**: número sequencial protegido por `UniqueConstraint(ticket_type, ticket_date,
  sequence)` + retry (3 tentativas); "chamar próximo" usa UPDATE condicional para dois atendentes
  simultâneos não chamarem a mesma senha.

## Desenvolvimento

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest
```
