# queue-totem-core — Plano do Projeto

> Este documento existe para dar contexto completo a quem (ou qual agente de IA) for continuar
> este projeto a partir de um chat novo, sem acesso ao histórico da conversa onde ele foi
> concebido. Leia por completo antes de tomar qualquer decisão de implementação.

## 1. Origem e motivação

Este pacote nasceu de uma necessidade concreta do projeto **UBS Veterinária de Paço do
Lumiar/MA** (`~/cenoradev/semitur/ubs-pet`), que precisava implementar um módulo de **fila de
atendimento presencial com totem físico** (emissão de senha AG/TR/FM, painel de chamada em TV,
painel de gerenciamento pela recepção).

Ao planejar esse módulo, ficou identificado que **pelo menos mais 2 projetos futuros** do mesmo
desenvolvedor (todos hospedados na mesma VPS, `vps-contabo`) muito provavelmente precisarão de uma
funcionalidade praticamente idêntica: qualquer serviço público de atendimento presencial (postos de
saúde, unidades de atendimento ao cidadão, etc.) tende a ter o mesmo problema — senha física,
prioridade de atendimento, painel de chamada, fila gerenciada pela recepção.

**Decisão tomada:** em vez de implementar esse módulo do zero em cada projeto (com o risco real de
divergência de lógica e retrabalho de manutenção), ele será construído **uma única vez, aqui,
como um pacote Python instalável via git**, e cada projeto que precisar dele (incluindo o
`ubs-pet`, que é o primeiro consumidor) apenas o instala como dependência.

## 2. Escopo: só backend, propositalmente

Foi cogitado (e descartado) empacotar também o frontend (componentes React de totem/painel de TV).
**Decisão explícita: este projeto é backend-only.**

Motivo: o totem e o painel de chamada são telas **voltadas ao cidadão**, expostas fisicamente na
recepção de cada unidade — elas precisam ter a identidade visual (logo, cores institucionais, nome
da unidade) de cada projeto/secretaria que as usa. Generalizar isso a ponto de ser reutilizável sem
parecer "genérico demais" custaria mais esforço de engenharia (sistema de temas, slots de
logo/branding, textos configuráveis) do que simplesmente reescrever 2-3 telas React relativamente
simples em cada novo projeto — telas que, de qualquer forma, só consomem uma API JSON já pronta.

Onde está o valor real de reutilização é na **lógica de backend**, que é idêntica
independentemente de branding: geração seringa seguro de senha sequencial diária, ordenação por
prioridade, ciclo de vida de status, regras de "chamar novamente"/"não compareceu". Essa lógica é
não-trivial o suficiente para valer a pena centralizar, e 100% invisível ao usuário final — logo,
sem risco de "carinha genérica" nas telas do cidadão.

**Conclusão prática:** este repositório entrega uma API REST (FastAPI) + modelos de dados
(SQLAlchemy), sem nenhum componente de UI. Cada projeto consumidor escreve sua própria interface
(totem, painel de TV, painel de recepção) chamando essa API.

## 3. Stack e compatibilidade obrigatória

Os projetos que vão consumir este pacote (confirmado em `ubs-pet` e `nievs-dashboard`, ambos do
mesmo desenvolvedor) usam:

| Camada | Tecnologia | Versão |
|---|---|---|
| Framework web | FastAPI + Uvicorn | 0.104+ |
| ORM | SQLAlchemy 2.0 async | 2.0.35+ |
| Driver Postgres | asyncpg | 0.29+ |
| Validação | Pydantic v2 | 2.5+ |
| Migrations | Alembic | 1.13+ — dependência do pacote desde a v0.3.0; cadeia **própria**, com `version_table` separado, que **não** se integra à do host (ver 5.4) |
| Runtime | Python | 3.11 |

Este pacote **deve declarar dependências compatíveis com essas versões** (não fixar versões mais
novas que quebrem compatibilidade com os projetos consumidores existentes) e assumir que o host
roda um FastAPI app com SQLAlchemy async + Postgres já configurado.

## 4. Distribuição e versionamento

- **Sem PyPI.** Distribuição via dependência git, instalada diretamente do GitHub.
- **Repositório público no GitHub** (recomendação forte, pendente de confirmação final do
  desenvolvedor — ver seção 8 "Decisões pendentes"). Justificativa: não há dado sensível de
  domínio aqui (é lógica genérica de fila/senha), e um repo público elimina a necessidade de
  configurar SSH deploy keys ou GitHub tokens como build secret em todo Dockerfile de todo projeto
  consumidor — `pip install git+https://github.com/...` funciona sem autenticação nenhuma.
- **Versionamento por tag semver** (`v0.1.0`, `v0.2.0`, `v0.3.0`, ...). Cada projeto consumidor
  fixa uma tag específica no `requirements.txt`, nunca aponta para `main`/`HEAD`:
  ```
  queue-totem-core @ git+https://github.com/<usuario>/queue-totem-core.git@v0.1.0
  ```
- Alternativa via SSH (se o repo acabar sendo privado):
  ```
  queue-totem-core @ git+ssh://git@github.com/<usuario>/queue-totem-core.git@v0.1.0
  ```
  exige chave SSH com acesso de leitura montada no contexto de build do Docker de cada projeto
  consumidor — mais fricção operacional, evitar se possível.

## 5. Princípios de arquitetura (não negociáveis)

Estes princípios existem para que o pacote funcione em qualquer projeto host **sem que o pacote
precise conhecer nada sobre o domínio de negócio do host**. A direção de dependência é sempre
host → pacote, nunca o contrário.

### 5.1 Router como factory function, não router fixo

O pacote não pode hardcodar papéis/roles de autorização (ex: `require_role("admin",
"recepcionista")`) porque cada projeto host tem seu próprio sistema de roles, diferente do
próximo. A auth é **injetada** pelo host na hora de montar o router:

```python
from queue_totem_core import build_queue_router, QueueConfig

router = build_queue_router(
    get_db=app_get_db,                      # dependency do host para obter AsyncSession
    manage_dependency=require_role("admin", "recepcionista"),  # quem pode chamar/gerenciar a fila
    read_dependency=get_current_user,       # quem pode listar a fila (autenticado, qualquer perfil)
    config=QueueConfig(...),                # ver 5.3
)
app.include_router(router, prefix="/queue", tags=["queue"])
```

Os endpoints de **emissão de senha (totem)** e **consulta do painel de TV** são sempre públicos,
sem nenhuma dependency de autenticação — são dispositivos físicos sem tela de login. Isso é fixo no
pacote (não configurável), pois é um requisito estrutural do caso de uso (totem/painel não têm como
autenticar um cidadão).

### 5.2 Nenhuma referência a entidades do host — apenas string opaca

O caso de origem (`ubs-pet`) tem um tipo de senha ("AG") vinculado a um `Appointment` (agendamento)
do domínio pet. O pacote **não pode conhecer o conceito de "agendamento"** — ele expõe apenas um
campo genérico:

```python
reference_code: Optional[str]   # ex.: número de protocolo, código de referência, o que for
reference_label: Optional[str]  # rótulo livre para exibição (ex.: nome do paciente/cliente)
```

A validação de que essa referência existe e é válida (ex.: "esse protocolo é de um agendamento
confirmado para hoje?") é **responsabilidade do host**, feita *antes* de chamar o endpoint de
emissão de senha do pacote — o pacote apenas armazena o que foi passado, sem validar semântica de
domínio.

### 5.3 Tudo relevante ao negócio é configurável via objeto de config

```python
class TicketTypeConfig(BaseModel):
    code: str                       # ex.: "AG", "TR", "FM" — mas podem ser quaisquer strings
    label: str                      # rótulo de exibição
    priority_source: Literal["external", "self_declared", "none"]
    # "external"     -> prioridade vem de fora (o host informa is_priority=True/False ao emitir)
    # "self_declared" -> quem emite a senha (o totem) declara a prioridade no momento da emissão
    # "none"          -> tipo nunca é prioritário

class StationConfig(BaseModel):          # v0.3.0
    code: str                            # ex.: "recepcao", "triagem" — string opaca
    label: str

class QueueConfig(BaseModel):
    ticket_types: list[TicketTypeConfig]
    priority_order: list[tuple[str, bool]]   # lista ordenada de (ticket_type, is_priority) -> rank
    daily_reset: bool = True                 # sequência reinicia a cada dia?
    max_recall_attempts: int = 1             # quantas vezes pode "chamar novamente" antes de nao_compareceu
    normals_per_priority: int = 0            # 0 = prioridade estrita; N = 1 prioritário a cada N normais
    stations: list[StationConfig] | None = None   # None = modo estação única (v0.2.0)
    entry_station: str | None = None              # obrigatório quando stations é declarado
```

Exemplo de configuração equivalente ao caso de uso original do `ubs-pet` (útil como referência,
**não deve ser hardcoded no pacote** — é só um exemplo de config que o host `ubs-pet` vai passar):

```python
QueueConfig(
    ticket_types=[
        TicketTypeConfig(code="AG", label="Agendamento", priority_source="external"),
        TicketTypeConfig(code="TR", label="Triagem", priority_source="self_declared"),
        TicketTypeConfig(code="FM", label="Farmácia", priority_source="none"),
    ],
    priority_order=[("AG", True), ("AG", False), ("TR", True), ("TR", False), ("FM", False)],
    daily_reset=True,
    max_recall_attempts=1,
)
```

### 5.4 Persistência isolada — cadeia Alembic própria, separada da do host

O pacote define seu **próprio `Base`/metadata** (não compartilha `Base` com o host) e é dono
exclusivo da tabela `queue_tickets`, sem FK para fora.

Até a v0.2.0 o schema nascia de `create_all`, via `await init_queue_tables(engine)` chamado pelo
host na inicialização. Isso funciona para criar, mas **`create_all` não altera tabela existente** —
uma coluna nova numa versão futura não apareceria sozinha em nenhum dos projetos já rodando. A
v0.3.0 fecha esse buraco versionando o próprio schema:

- **Cadeia Alembic dentro do pacote** (`queue_totem_core/migrations/`), com `version_table`
  fixado em **`queue_versions_table`** — nunca `alembic_version`. Duas cadeias apontando para a
  mesma tabela destroem o histórico uma da outra, e o host tem a dele no mesmo banco. O nome é
  fixo no `env.py` do pacote: vale igual para todos os projetos, não é convenção por instalação.
- **`include_object` blinda o host**: o autogenerate do pacote só enxerga as tabelas do próprio
  metadata.
- **Brownfield é caso de primeira classe**: num banco onde `queue_tickets` já existe (criada por
  `create_all`, sem nenhuma linha de controle de versão), a revisão base detecta a tabela e faz
  apenas o *stamp*, sem recriar nada; só o delta é aplicado. Num banco vazio, cria tudo.
- **Roda no deploy, nunca no boot** — `python -m queue_totem_core.migrate upgrade head`, em
  container avulso, ao lado do `alembic upgrade head` do host. Migration dentro do `lifespan`
  daria ao boot da aplicação o poder de alterar schema.
- `init_queue_tables` **continua existindo** para bancos novos e desenvolvimento, mas deixa de ser
  o mecanismo de evolução.

O que continua valendo: **não integrar à cadeia Alembic do host**. Acoplar o versionamento do
pacote ao histórico de migrations de cada projeto é frágil e desnecessário para uma tabela isolada.

### 5.4.1 Estações: N filas paralelas, roteamento do host (v0.3.0)

Até a v0.2.0 o pacote modelava **uma fila só**: `POST /tickets/next` varria todas as senhas do dia
e devolvia a próxima pelo `priority_order` global. Com recepção, triagem, consultórios, banho e
farmácia chamando ao mesmo tempo, todos puxariam do mesmo balde.

A fronteira da v0.3.0: **o pacote é o motor de filas; quem decide para qual estação alguém vai é o
host.** Uma `station` é string opaca declarada em `QueueConfig`, na mesma filosofia dos
`ticket_types` — o pacote não define quais estações existem nem a ordem entre elas.

O ticket ganha três campos, todos nullable:

- `station` — estação atual; nasce em `config.entry_station`.
- `station_entered_at` — quando entrou na estação atual, para medir espera por etapa.
- `queued_since` — **carimbo de chegada original, imutável ao longo da jornada**.

`queued_since` resolve um problema de justiça real: se a ordem de cada fila usasse a entrada na
estação, quem esperou 40 minutos na recepção entraria no fim da fila do consultório. O FIFO de
qualquer estação usa `queued_since` (com fallback para `created_at` em senhas anteriores à v0.3.0).

Uma estação pode ter mais de um posto atendendo ao mesmo tempo. `room` (v0.4.0) é a string opaca
que diz de onde a chamada saiu, gravada a cada `POST /tickets/next` e devolvida pelo painel. Ela
pertence à **chamada**, não à senha: mudar de estação a limpa, porque anunciar o posto da etapa
anterior manda a pessoa para o lugar errado. O pacote não sabe quais postos existem — quem declara é
o host, como em tudo mais.

`PATCH /tickets/{id}/station` é o coração da mudança — expressa "terminou na recepção, foi para a
triagem": move a senha, devolve o status para `na_fila`, preserva `queued_since`, zera o estado de
chamada (`called_at`, `recall_count`) e responde com o tempo gasto na estação anterior. O pacote
não guarda histórico de etapas: quem quiser auditar a jornada persiste esse dado no próprio
domínio.

**Compatibilidade retroativa é requisito**, não cortesia — o pacote atende outros projetos. Sem
`stations`, tudo opera em modo estação única, `/tickets/next` sem `station` mantém o comportamento
global, `GET /display` devolve exatamente o JSON da v0.2.0, e os parâmetros/endpoints de estação
respondem 400. Por isso a release é **minor**.

Não foi introduzida classificação de urgência — descartada no levantamento, e decisão do host se um
dia existir.

### 5.5 Ciclo de vida de status (fixo — é a lógica central do pacote)

```
na_fila → chamado → em_atendimento → concluido
                  ↘ nao_compareceu (após esgotar max_recall_attempts)
```

- Senha nasce direto em `na_fila` (sem estado intermediário morto tipo "aguardando").
- "Chamar próximo": seleciona o ticket elegível de maior prioridade (via `priority_order`) + FIFO
  dentro da mesma faixa de prioridade (`created_at` crescente), muda para `chamado`.
  Se `normals_per_priority > 0`, aplica intercalação justa: a cada N normais chamadas em
  sequência, a próxima chamada é de um prioritário aguardando (evita que uma enxurrada de
  prioritários trave a fila normal).
- "Chamar novamente": só permitido em `status == "chamado"`; incrementa contador de recall; ao
  atingir `max_recall_attempts`, uma nova tentativa deve ser rejeitada (400) sugerindo marcar como
  `nao_compareceu`.
- "Não compareceu": só permitido em `status == "chamado"`.
- `em_atendimento`/`concluido`: transições manuais expostas via endpoint autenticado — pensadas
  para o host acionar quando o atendimento efetivamente começa/termina (ex.: quando o `ubs-pet`
  tiver seu módulo de Prontuário Clínico implementado, essa transição pode ser automatizada lá).

### 5.6 Geração de número sequencial diário — segurança de concorrência

Abordagem recomendada (já validada em produção no padrão equivalente do `ubs-pet`, para
`Appointment.protocol`): contar registros existentes do dia/tipo via `SELECT COUNT(...)`, tentar
inserir com um `UniqueConstraint(ticket_type, ticket_date, sequence)`, capturando `IntegrityError`
em loop de até 3 tentativas com rollback+retry. **Não** introduzir uma tabela de contador separada
nem locks explícitos (`SELECT ... FOR UPDATE`) — é complexidade desnecessária para o volume
esperado (um totem físico por unidade, dezenas de senhas por dia, não milhares por segundo).

## 6. Superfície da API (endpoints esperados)

| Verbo | Path | Acesso | Responsabilidade |
|---|---|---|---|
| POST | `/tickets` | Público | Totem emite senha. Recebe `ticket_type`, `reference_code` (opcional), `is_priority`/`priority_reason` (se `priority_source == "self_declared"`). Gera `ticket_number` sequencial do dia. |
| GET | `/display` | Público | Painel de TV: ticket atualmente chamado + últimos N chamados. Pensado para polling (não há infraestrutura de websocket assumida). Com estações, acrescenta `stations[]` — um chamado corrente por estação — mantendo `current` para não quebrar painéis da v0.2.0. |
| GET | `/tickets` | Autenticado (`read_dependency`) | Lista a fila do dia, ordenada por prioridade + FIFO. Filtro opcional `?station=`. |
| POST | `/tickets/next` | Autenticado (`manage_dependency`) | "Chamar próximo" — aplica a lógica de prioridade. `?station=` opcional: sem ele, comportamento global da v0.2.0. |
| PATCH | `/tickets/{id}/recall` | Autenticado (`manage_dependency`) | "Chamar novamente", respeitando `max_recall_attempts`. |
| PATCH | `/tickets/{id}/no-show` | Autenticado (`manage_dependency`) | Marca "não compareceu". |
| PATCH | `/tickets/{id}/status` | Autenticado (`manage_dependency`) | Transição manual para `em_atendimento`/`concluido`. |
| PATCH | `/tickets/{id}/station` | Autenticado (`manage_dependency`) | v0.3.0 — move a senha para outra estação, devolvendo-a a `na_fila`. 400 em modo estação única. |

## 7. Caso de uso de referência (contexto, não requisito literal)

Para entender a origem funcional completa, veja o caso de uso TASK-VET-11 do projeto `ubs-pet`
(`~/cenoradev/semitur/ubs-pet/docs/Casos_de_Uso_UBS_PET.md`): cidadão se aproxima do totem, escolhe
AG/TR/FM, sistema emite senha (ex.: `AG-001`), recepcionista chama por ordem de prioridade
(`AG-CadÚnico > AG-Normal > TR-Preferencial > TR-Normal > FM`), painel de TV exibe a chamada com
som, e há uma regra de "protocolo não encontrado" (quando a senha AG não corresponde a nenhum
agendamento válido — nesse caso, **a decisão de design foi: rejeitar a emissão, não converter
silenciosamente para TR** — essa é uma decisão que o host toma ao validar a `reference_code` antes
de chamar o pacote, não algo que o pacote decide sozinho).

Este caso de uso é a motivação e o teste de aceitação mental do design, mas **o pacote não deve
hardcodar nenhum desses nomes de tipo (AG/TR/FM) ou textos** — eles chegam via `QueueConfig`.

## 8. Decisões pendentes (confirmar com o usuário antes de seguir)

- **Visibilidade do repositório GitHub**: recomendação é público (ver seção 4), mas ainda não
  confirmado explicitamente pelo usuário. Perguntar antes de fazer `gh repo create`.
- **Nome final do pacote Python** (distribuição vs. import): sugestão é distribuição
  `queue-totem-core` (bate com o nome da pasta já criada) e nome de import `queue_totem_core`
  (underscore, convenção Python). Confirmar se o usuário quer outro nome.
- **Ferramenta de build do pacote**: ainda não escolhida. Recomendação: `pyproject.toml` com
  `setuptools` (mais simples e universalmente compatível, sem necessidade de ferramentas extras).
  Alternativa: `hatchling` (mais moderno). Qualquer uma resolve — decisão de baixo risco, pode ser
  definida diretamente pelo próximo agente sem precisar perguntar ao usuário, a menos que haja
  preferência explícita.

## 9. Próximos passos sugeridos (para o próximo chat/agente)

1. `git init` neste diretório (ainda não é um repositório git).
2. Estrutura inicial de pacote:
   ```
   queue-totem-core/
   ├── src/queue_totem_core/
   │   ├── __init__.py          # exporta build_queue_router, QueueConfig, TicketTypeConfig
   │   ├── models.py             # Base próprio + modelo QueueTicket
   │   ├── schemas.py            # Pydantic: QueueTicketCreate/Out, QueueConfig, TicketTypeConfig
   │   ├── router.py             # build_queue_router(...)
   │   ├── priority.py           # lógica de ranking de prioridade
   │   └── db.py                 # init_queue_tables(engine) / helpers de criação de tabela
   ├── tests/
   ├── pyproject.toml
   ├── README.md
   └── docs/PLANO.md             # este arquivo
   ```
3. Implementar conforme os princípios da seção 5, com testes cobrindo: geração de senha sequencial
   sob concorrência simulada, ordenação de prioridade, transições de status (incluindo bloqueio de
   recall além do limite).
4. Confirmar com o usuário as "Decisões pendentes" da seção 8.
5. Criar o repositório no GitHub (`gh repo create`, com confirmação explícita do usuário antes de
   executar — é uma ação visível/externa) e publicar a primeira tag (`v0.1.0`).
6. Voltar ao projeto `ubs-pet` e substituir o plano original do módulo de fila (que assumia
   implementação acoplada local, em `~/.claude/plans/vamos-trabalhar-com-o-elegant-teapot.md`) por
   uma integração que instala este pacote via `pip install "git+https://.../queue-totem-core.git@v0.1.0"`
   e monta o router com a config/roles específicas do `ubs-pet`.
