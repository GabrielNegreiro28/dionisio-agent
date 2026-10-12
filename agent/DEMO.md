# Demo walkthrough

This walkthrough shows planning, computation, confirmation, and capability boundaries. The current CLI interactions are in Portuguese; the explanations below are in English.

Complete [developer setup](README.md), then run from `agent/`:

```powershell
.\.venv\Scripts\python.exe main.py --debug
```

Use a test environment. Data-dependent examples can return different records as the service changes.

## 1. Retrieve and filter customers

```text
Lista os clientes que gastaram mais de R$500 no último mês e nunca usaram cupom.
```

**Meaning:** list customers who spent more than R$500 in the last month and have never used a coupon.

Inspect the debug output for customer retrieval, Python filtering, and counting. The response should describe the resulting records and any limitations in the available data.

## 2. Ask about product behavior

```text
Como funciona o no-show no Dionísio?
```

**Meaning:** how does no-show handling work in Dionisio?

This request exercises the product knowledge path rather than a record mutation.

## 3. Inspect a capability boundary

```text
O prato 'Risoto de Funghi' saiu do cardápio, remove ele e avisa quem pediu nos últimos 7 dias.
```

**Meaning:** remove an unavailable dish and notify people who ordered it in the last seven days.

The exposed catalog does not include menu management or outbound notifications. Observe how the assistant communicates those limits and any supported retrieval work without claiming that the full request was completed.

## 4. Optional: exercise confirmation

Use a disposable reservation in the external test service. Inspect available records with:

```powershell
.\.venv\Scripts\python.exe probe.py reservations_list
```

Replace the placeholder with an actual eligible test reservation ID:

```text
Cancela a reserva de id <reservation_id>
```

First respond `não` to reject the pending cancellation. Repeat the request and respond `confirmo` only if you intend to cancel that test record. Approval changes service data.

Inspect the confirmation prompt, the chosen entity, and the reported execution result.

## 5. Ask an out-of-scope question

```text
Qual a previsão do tempo amanhã?
```

**Meaning:** what is tomorrow's weather forecast?

Observe whether the assistant makes the CRM scope and missing capability clear.

## Capture evidence

A useful portfolio demo can include a short recording of the read-only flow, the generated plan, and the resulting response. Redact credentials and customer data from public captures. Publish actual execution evidence with its environment and date.

Enter `sair` to exit.
