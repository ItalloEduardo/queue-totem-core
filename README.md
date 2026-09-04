# queue-totem-core

Fila de atendimento presencial (senha de totem, painel de chamada, gestão pela recepção) como um
**router FastAPI plugável**, agnóstico de domínio.

Contexto completo de design e decisões: [`docs/PLANO.md`](docs/PLANO.md).

## Instalação

Sem PyPI — dependência git fixada por tag semver:

```
queue-totem-core @ git+https://github.com/<usuario>/queue-totem-core.git@v0.2.0
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
    await init_queue_tables(engine)  # cria a tabela do pacote (fora do Alembic do host)
```

## Endpoints

| Verbo | Path | Acesso | Função |
|---|---|---|---|
| POST | `/tickets` | Público | Totem emite senha (número sequencial do dia por tipo) |
| GET | `/display` | Público | Painel de TV: senha chamada atual + últimas N (polling) |
| GET | `/tickets` | `read_dependency` | Fila do dia, ordenada por prioridade + FIFO (`?status=` opcional) |
| POST | `/tickets/next` | `manage_dependency` | Chamar próxima senha (aplica `priority_order` + `normals_per_priority`) |
| PATCH | `/tickets/{id}/recall` | `manage_dependency` | Chamar novamente (respeita `max_recall_attempts`) |
| PATCH | `/tickets/{id}/no-show` | `manage_dependency` | Marcar não compareceu |
| PATCH | `/tickets/{id}/status` | `manage_dependency` | Transição manual: `em_atendimento` / `concluido` |

## Política de prioridade ("chamar próximo")

`priority_order` define o ranking de `(tipo, is_priority)` e o desempate é FIFO
(`created_at`). Como isso combina prioritários e normais depende de `normals_per_priority`:

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
- **Persistência**: o pacote tem `Base`/metadata próprios e cria a tabela `queue_tickets` via
  `init_queue_tables(engine)`. Não integrar ao Alembic do host.
- **Concorrência**: número sequencial protegido por `UniqueConstraint(ticket_type, ticket_date,
  sequence)` + retry (3 tentativas); "chamar próximo" usa UPDATE condicional para dois atendentes
  simultâneos não chamarem a mesma senha.

## Desenvolvimento

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest
```
