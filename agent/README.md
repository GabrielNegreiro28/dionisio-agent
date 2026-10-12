# Developer setup

For the project overview, see the [main README](../README.md). Design decisions are documented in [ARCHITECTURE.md](ARCHITECTURE.md), and runnable examples are in [DEMO.md](DEMO.md).

## Prerequisites

- Python 3.10 or newer.
- An OpenRouter API key.
- A Dionisio CRM test API key and access to the external service.

The project uses Python orchestration, Pydantic-validated plans, and OpenRouter-hosted language models. Conversational examples and the current runtime interface are in Portuguese.

## Install on Windows

From the repository root:

```powershell
Set-Location .\agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Configure these variables in your local `.env`:

| Variable | Purpose |
| --- | --- |
| `DIONISIO_API_KEY` | Authentication for the external CRM test API. |
| `OPENROUTER_API_KEY` | Authentication for the LLM provider. |
| `LLM_MODEL` | Main planning/response model ID supported by your OpenRouter account. |
| `LLM_MODEL_FAST` | Optional model override for lighter orchestration tasks. |

Review the defaults in `config.py` and replace model IDs as needed for your provider access. Keep credentials in `.env`; commit only the credential-free example file.

## Install on Linux or macOS

```bash
cd agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
# Edit .env with your credentials and supported model IDs.
.venv/bin/python main.py --debug
```

## Run on Windows

```powershell
.\.venv\Scripts\python.exe main.py
# Add --debug to inspect execution.
.\.venv\Scripts\python.exe main.py --debug
```

Enter `sair` to leave the interactive session.

## Benchmark and tests

These commands run from `agent/` with the environment configured:

```powershell
# Read-only benchmark by default; generates benchtest_report.md locally.
.\.venv\Scripts\python.exe benchtest.py

# Run a focused benchmark selection.
.\.venv\Scripts\python.exe benchtest.py --only spend

# Focused automated tests.
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tools/test_openapi_sync.py execution/test_data_ops.py planning/test_plan_rules.py -q
```

`benchtest.py --mutating` also exercises destructive actions. Use disposable records in the test service for that mode. Live runs depend on API availability, credentials, and model access.

## Inspect the API

```powershell
.\.venv\Scripts\python.exe probe.py tools
.\.venv\Scripts\python.exe probe.py clients_inactive days=60
.\.venv\Scripts\python.exe probe.py dump --out ./_dump
```

Dumped records can contain service data. Keep local inspection output out of version control.

## Product knowledge

Product documentation is bundled in `knowledge/dionisio_docs.md`. To run the ingestion utility:

```powershell
.\.venv\Scripts\python.exe -m knowledge.ingest
```

## Troubleshooting

- **Missing API key:** check the local `.env` and run from `agent/`.
- **Model unavailable:** set a supported `LLM_MODEL` / `LLM_MODEL_FAST` in `.env`.
- **Service/authentication error:** verify CRM credentials and external service access.
- **Unsupported operation:** inspect the tool catalog; the agent's capabilities are bounded by the exposed API.
