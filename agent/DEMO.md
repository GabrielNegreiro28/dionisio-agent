# Dionísio Agent — Roteiro de Demonstração

Demonstração de ~2-3 minutos rodando o agente no terminal. O modo `--debug` mostra o **plano
gerado** (nós `api` / `compute` / `verify` / `fanout`, classificação e gate de risco), então o
avaliador vê a arquitetura determinística operando, não só a resposta final.

## Antes de gravar

1. `.env` configurado (ver `README.md`). Para a gravação, um modelo barato e rápido basta:
   ```
   LLM_MODEL=openai/gpt-4o-mini
   ```
2. Pegue um id de reserva real para o passo destrutivo:
   ```bash
   python probe.py reservations_list
   ```
   Anote um `id` (ex: `res_xxxxxxxx`) com status ativo (`pending`/`confirmed`/`seated`).
3. Inicie a sessão:
   ```bash
   python main.py --debug
   ```

## Como gravar

Terminal (recomendado), com asciinema — gera um `.cast` leve e embutível:
```bash
pip install asciinema          # uma vez
asciinema rec demo.cast        # começa a gravar
#  ... rode o roteiro abaixo ...
#  Ctrl-D para parar
asciinema play demo.cast       # revisar
```
Alternativa: qualquer gravador de tela (OBS, ou Win+G no Windows) capturando a janela do terminal.

Dica: narre uma frase por passo dizendo o que ele prova.

---

## Roteiro (5 passos)

### 1. Leitura com agregação — determinismo, sem alucinar
Digite:
```
Lista os clientes que gastaram mais de R$500 no último mês e nunca usaram cupom.
```
Aponte no debug: o plano é `clients_top_spenders → compute filter → compute count`. A filtragem e
a contagem são feitas por código (`data-op`), não pelo modelo. A resposta lista exatamente os
clientes e o total bate com a lista — sem o problema de "diz 10 e lista 9".

### 2. Operação destrutiva — gate de risco e confirmação (os dois caminhos)
Digite (use o id que você anotou):
```
Cancela a reserva de id res_xxxxxxxx
```
O agente devolve um pedido de confirmação com os parâmetros humanizados. Primeiro **recuse**:
```
não
```
Resposta: "Operação cancelada conforme solicitado." (nada foi alterado). Agora repita o comando e
**confirme**:
```
Cancela a reserva de id res_xxxxxxxx
confirmo
```
Aponte: ação irreversível (`x-destructive`) nunca roda na primeira interação; só executa após o
"confirmo", e a escrita acontece por último (execução em duas fases).

### 3. Fulfillment parcial / recusa — a falha tratada com honestidade
Digite:
```
O prato 'Risoto de Funghi' saiu do cardápio, remove ele e avisa quem pediu nos últimos 7 dias.
```
Aponte: o agente faz o que é possível (busca os pedidos do período) e declara o que **não** tem
ferramenta — remover item do cardápio e enviar mensagens — em `unsupported`, oferecendo um rascunho.
Não falha o pedido inteiro nem finge ter executado.

### 4. Conhecimento ancorado na documentação (bônus)
Digite:
```
Como funciona o no-show no Dionísio?
```
Aponte: a resposta vem da documentação oficial (GitBook), com indicação da seção, em vez de
conhecimento genérico.

### 5. Fora de escopo — recusa limpa
Digite:
```
Qual a previsão do tempo amanhã?
```
Aponte: o agente recusa de forma direta, sem inventar um dado que não tem.

Encerre com `sair`.

---

## Observações

- O passo 2, ao confirmar, **cancela uma reserva real** do ambiente de teste — esperado numa demo;
  use o caminho de recusa se preferir não alterar dados.
- A amplitude de casos é coberta pelo `benchtest.py` (31 cenários, leitura/destrutivo/borderline/
  conhecimento); esta demo é a fatia curada e visual desse conjunto.
