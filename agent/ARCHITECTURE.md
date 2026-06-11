# Arquitetura — Dionísio Agent

Agente de CRM para operadores de restaurante. Python puro, sem framework de agentes —
cada camada é explícita, testável e auditável.

**Resultado do benchtest: 31 cenários, 127/127 checagens ✅** (leituras com oráculos
contra a API, fluxo destrutivo com gate, edge cases, multi-turno). Ver `benchtest_report.md`,
que inclui telemetria de custo e latência por pedido.

## Pipeline

```
mensagem do operador
        │
        ▼
┌─────────────────┐   só com histórico: resolve "dele", "essa reserva"…
│ 1. REWRITER      │   (modelo rápido; pulado no 1º turno — zero custo)
└─────────────────┘
        │
        ▼
┌─────────────────┐   regras por palavra-chave + fallback LLM (modelo rápido)
│ 2. CLASSIFIER    │   domínios: clients, reservations, orders, coupons, …
└─────────────────┘
        │
        ▼
┌─────────────────┐   catálogo restrito aos domínios (prompt menor/mais barato);
│ 3. TOOL SELECT   │   fallback: catálogo COMPLETO se o plano falhar
└─────────────────┘
        │
        ▼
┌─────────────────┐   LLM gera um GRAFO TIPADO de nós (JSON validado por Pydantic):
│ 4. PLANNER       │   api | compute | verify | fanout
└─────────────────┘   1 tentativa de auto-reparo se o JSON vier inválido
        │
        ▼
┌─────────────────┐   duas fases: SENSE (reversível) → ACT (escritas por último)
│ 5. EXECUTOR      │   risk gate em toda chamada; confirmação p/ destrutivas;
└─────────────────┘   replan com catálogo completo se um passo falhar
        │
        ▼
┌─────────────────┐   LLM APENAS DESCREVE resultados já calculados —
│ 6. RESPONSE      │   nunca conta, filtra ou soma (anti-alucinação)
└─────────────────┘
```

## Decisões de projeto e trade-offs

| Decisão | Por quê | Trade-off aceito |
|---|---|---|
| **Plano tipado (grafo JSON) em vez de loop ReAct** | Determinismo e auditabilidade: o plano inteiro é validado ANTES de executar; impossível "alucinar" uma tool no meio da execução | Menos flexível para tarefas exploratórias; replan cobre falhas |
| **Nós `compute` determinísticos (filter/find/count/sort…)** | Tira contagem/filtragem do LLM — números na resposta vêm de código puro, não de geração de texto | O planner precisa aprender o vocabulário de ops (mitigado com exemplos no prompt) |
| **Execução em duas fases (SENSE → ACT)** | Nenhuma escrita acontece antes de TODAS as leituras/verificações passarem; pré-valida cada escrita antes da primeira mutação | Escritas que alimentam passos posteriores não podem ser adiadas (ficam inline) |
| **Risk gate + "confirmo" para destrutivas** | Ação irreversível nunca executa sem confirmação explícita; prompt mostra a entidade real ("João Silva", não só o id) | Um turno a mais de latência em operações destrutivas — de propósito |
| **Resposta render-only (só descreve)** | A causa nº 1 de números errados em agentes é o LLM "recontar" — aqui ele recebe valores prontos e é instruído a não derivar nada | Pedidos cujo recorte não foi planejado retornam "não foi apurado" em vez de um chute |
| **Desambiguação em `find` sensível** | `find` pega o 1º match — ok para leitura, inaceitável antes de cancelar/remarcar; com 2+ matches o agente PERGUNTA em vez de escolher | Um turno a mais quando há homônimos |
| **Sem retry em escritas** | Um 5xx pode chegar DEPOIS do servidor ter persistido; sem chave de idempotência na API, retry cego pode duplicar efeito | Falha transitória numa escrita vira falha relatada (com oferta de compensação) |
| **Modelo rápido para tarefas triviais** | Rewriter/classifier/resumo de memória não precisam do modelo principal — só pagariam latência e custo | Dois modelos para configurar (`LLM_MODEL`, `LLM_MODEL_FAST`) |
| **Fanout paralelo (thread pool, só leituras)** | 31 chamadas dia-a-dia caem de ~90s para poucos segundos; escrita por elemento é BLOQUEADA (furaria a confirmação) | Limite de 8 conexões simultâneas contra a API |
| **Sem framework (LangChain etc.)** | O caso pede raciocínio sobre segurança e arquitetura — cada decisão acima é visível no código, não escondida numa abstração | Mais código próprio para manter |

## Tratamento de falhas (o que acontece quando algo dá errado)

| Falha | Comportamento |
|---|---|
| Plano JSON inválido | 1 auto-reparo (modelo vê o próprio erro); depois, falha limpa em PT-BR |
| Classifier perdeu um domínio | Replan com o catálogo COMPLETO de tools |
| Passo falha sem escrita anterior | Replan (até `MAX_REPLANS`) com resultados parciais como contexto |
| Passo falha APÓS uma escrita | Status `partial`: lista o que foi feito + oferece compensação (ex: cupom criado → `coupons_deactivate`) |
| Precondição `verify` falsa | `halted` com a mensagem do operador — nada irreversível aconteceu |
| 2+ entidades para ação destrutiva | `needs_info`: lista os candidatos e pergunta qual |
| Resultado gigante (fanout longo) | Contexto truncado com marcador `__truncado__` (total real preservado) — resposta nunca vem vazia |
| Erro transitório em LEITURA | 1 retry; em ESCRITA, nunca (idempotência) |
| Mesma reserva cancelada 2x na sessão | Bloqueado pelo risk gate (checagem de estado) |

## Telemetria (unit economics)

Toda chamada LLM registra tokens, latência e custo (USD, via OpenRouter) em
`clients/telemetry.py`. O `benchtest_report.md` traz custo médio por pedido e
latência média/máxima da bateria; `python main.py --debug` mostra a telemetria
de cada turno. Em produção isso vira o dado-base para precificar o agente por
operação e dimensionar margem.

## Limitações conhecidas

- Sem ferramenta de mensagens/notificações e de gestão de cardápio — pedidos
  assim entram em `unsupported` e o agente faz o que é possível (fulfillment parcial).
- Desambiguação rastreia apenas referência direta `find → escrita destrutiva`;
  um `find` encadeado por outro compute não é detectado.
- Memória é por sessão (em processo); não persiste entre execuções.
- Catálogo de tools é estático (sincronizado com o contrato OpenAPI por teste).
