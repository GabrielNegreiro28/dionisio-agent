# Dionísio Agent — Setup

Agente de CRM para restaurante (Python puro). Guia de instalação e execução.

> **Benchtest: 31 cenários, 127/127 checagens ✅** — leituras validadas com oráculos
> contra a API, fluxo destrutivo com gate de confirmação, edge cases e multi-turno,
> com telemetria de custo/latência por pedido. Relatório: `benchtest_report.md`.
> Arquitetura e decisões de projeto: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## 1. Requisitos

- Python **3.10+**
- Uma API key do **OpenRouter** (`OPENROUTER_API_KEY`)
- A API key do mock Dionísio (`DIONISIO_API_KEY`)

## 2. Instalação

```bash
cd agent

# (recomendado) ambiente virtual
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

## 3. Configuração (`.env`)

Crie um arquivo `.env` dentro de `agent/`:

```env
DIONISIO_API_KEY=sua-key-do-mock
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-fable-5
```

| Variável | Obrigatória | Padrão | Observação |
|---|---|---|---|
| `DIONISIO_API_KEY` | sim | — | key do mock Dionísio |
| `OPENROUTER_API_KEY` | sim | — | key do OpenRouter |
| `LLM_MODEL` | não | `anthropic/claude-fable-5` | id do modelo no OpenRouter (ex: `anthropic/claude-opus-4-5`, `openai/gpt-4o-mini`) |
| `LLM_MODEL_FAST` | não | `anthropic/claude-haiku-4.5` | modelo barato para tarefas triviais (rewriter, classifier, memória) |

## 4. Rodar o agente

```bash
python main.py            # modo normal
python main.py --debug    # mostra domínios, plano e passos
```

Digite `sair` para encerrar.

## 5. (Opcional) Base de conhecimento

A documentação do produto (GitBook) já vem embutida em `knowledge/dionisio_docs.md`.
Para atualizar:

```bash
python -m knowledge.ingest
```

## 6. Testes

**Benchtest** (cenários ponta a ponta; precisa do `.env` configurado):

```bash
python benchtest.py                 # modo seguro (não altera dados)
python benchtest.py --mutating      # inclui ações destrutivas confirmadas
python benchtest.py --only spend    # subconjunto por id
```
Gera `benchtest_report.md`.

**Sync do catálogo com o contrato OpenAPI** e **unit tests** (precisam de `pytest`):

```bash
pip install pytest
python -m pytest tools/test_openapi_sync.py execution/test_data_ops.py planning/test_plan_rules.py -q
```

**Inspecionar a API mock direto** (verdade-base):

```bash
python probe.py tools                       # lista as ferramentas
python probe.py clients_inactive days=60     # chama uma tool
python probe.py dump --out ./_dump           # despeja as coleções
```
