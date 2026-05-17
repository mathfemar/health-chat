# health-chat

Bot de Telegram que analisa fotos de refeições e estima macros + calorias usando uma base nutricional real (TACO) em vez de só "chutômetro" da LLM.

> **Por que existe**: aplicativos como MyFitnessPal/FatSecret têm churn alto porque exigem abrir outro app. Aqui, o registro acontece dentro do messenger que você já usa o dia inteiro. Manda foto → resposta em ~5 segundos → corrige se quiser → soma no seu diário.

---

## Diferencial vs. apps de foto + LLM

Os apps comerciais (Cal AI, Bites.ai, etc.) jogam a foto inteira numa LLM e pedem macros já calculados. Resultado: números falsamente precisos baseados na memória paramétrica do modelo, que erra feio em porção e em alimentos brasileiros.

Aqui o pipeline é diferente:

1. **Vision LLM identifica** os alimentos visíveis (nome simples, em pt-BR) e estima a porção em gramas.
2. **Matcher** (Python + Postgres) procura cada alimento na **Tabela TACO** (UNICAMP, 597 alimentos).
3. **Calculator** multiplica `porção × macros por 100g` da TACO → números reais, auditáveis, com fonte.
4. Quando o matcher erra, **você corrige com um botão** e o bot aprende (`food_aliases`) — da próxima vez vai direto sem perguntar.

Resultado: você ganha precisão de tabela oficial + flexibilidade da LLM pra ler a foto.

---

## Arquitetura

```
┌────────────┐
│   foto     │
│ (Telegram) │
└─────┬──────┘
      │
      ▼
┌──────────────────────────────────┐
│  Vision LLM (Gemini via OR)     │  ← prompt: identifica items + porção em g
│  Output: [{name, portion_g, …}] │     decompõe pratos compostos
└─────┬────────────────────────────┘
      │
      ▼
┌──────────────────────────────────┐
│  Matcher (cascata)              │
│  1. food_aliases (aprendido)     │  ← exact match após normalizar
│  2. pg_trgm + similaridade       │  ← Postgres trigram, score 0-1
│  3. LLM rerank (Gemma 4 31B)      │  ← só se trigram for ambíguo
└─────┬────────────────────────────┘
      │
      ▼
┌──────────────────────────────────┐
│  Calculator                     │
│  portion_g / 100 × per_100g_*   │
└─────┬────────────────────────────┘
      │
      ▼
┌──────────────────────────────────┐
│  Resposta no Telegram           │
│  ✅ TACO  🟡 estimativa  ❌ none │
│  + botões: ✏️ trocar / 🗑 apagar  │
└──────────────────────────────────┘
                  │
                  │ usuário corrige
                  ▼
            salva em food_aliases (aprende)
```

---

## Stack

| Componente | Tecnologia |
|---|---|
| Bot | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.x |
| LLM | OpenRouter (Gemini 3 Flash pra visão, Gemma 4 31B pra rerank) |
| Banco | Postgres no Supabase (free tier) |
| Matching | `pg_trgm` (similaridade de trigramas) |
| Base nutricional | TACO 4ª ed. — NEPA/UNICAMP, 597 alimentos |

---

## Setup do zero

### Pré-requisitos
- Python 3.11+ (testado em 3.13)
- Conta no [Telegram](https://t.me/BotFather) (pra criar o bot)
- Conta no [Supabase](https://supabase.com) (free tier basta)
- Conta no [OpenRouter](https://openrouter.ai) (com $1-2 de crédito ou key free)

### 1. Bot do Telegram
1. Abre o [@BotFather](https://t.me/BotFather)
2. `/newbot` → segue as instruções → guarda o **token**
3. Manda `/setdescription` pra dar uma descrição
4. (Opcional) `/setuserpic` pra colocar imagem

### 2. Supabase
1. Cria um projeto novo em [supabase.com](https://supabase.com)
2. Escolhe região **South America (São Paulo)**
3. **Anota a senha do Postgres** (só aparece uma vez)
4. Quando subir: botão verde **Connect** (canto superior direito)
5. Aba **Connection string** → **URI** → modo **Transaction pooler** (porta 6543) OU **Direct connection** (porta 5432)
6. Substitui `[YOUR-PASSWORD]` pela senha do passo 3

### 3. OpenRouter
1. Cria conta em [openrouter.ai](https://openrouter.ai)
2. **Settings → Privacy**: habilita "Enable training and logging" (alguns modelos free exigem)
3. **Keys**: gera uma nova → guarda
4. **Credits**: adiciona $1-2 se for usar modelos pagos (Gemini Flash custa ~$0.003/foto)

### 4. Código

```powershell
# Clone / cd na pasta
git clone <repo> health-chat
cd health-chat

# Venv + deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Configura
cp .env.example .env       # ou copy no Windows
notepad .env               # preenche TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, DATABASE_URL
                           # ALLOWED_USER_ID pode ficar vazio (qualquer um pode usar)

# Importa base TACO (cria schema + popula 590 alimentos)
python import_taco.py
# Deve imprimir: "OK. Inseridos/atualizados: 590. Total no banco: 590."

# Sobe o bot
python bot.py
# Deve imprimir: "Bot rodando."

# No Telegram, manda /start pro seu bot
```

---

## Comandos do bot

| Comando | O que faz |
|---|---|
| `/start` | Boas-vindas, mostra seu `user_id` |
| `/ajuda` | Lista de comandos |
| `/hoje` | Refeições e total do dia |
| `/semana` | Total e média dos últimos 7 dias |
| `/apagar` | Remove a última refeição |
| `/modelo [slug]` | Vê ou troca o modelo de visão em runtime (sem reiniciar). Ex: `/modelo openai/gpt-4o` |
| `/buscar <termo>` | Busca direta no banco TACO. Útil pra ver o que o matcher acharia antes de tirar foto |

**Foto**: manda qualquer foto de comida. Em 3-8s o bot responde com breakdown + botões.

**Botões inline na resposta:**
- `✏️ trocar #N <item>` — abre as alternativas do TACO pra você escolher a correta. A escolha vira **alias permanente** — da próxima vez, o bot vai direto.
- `🗑 apagar refeição` — remove do diário.

---

## Estrutura de arquivos

```
health-chat/
├── bot.py                # Handlers do Telegram (commands, photo, callbacks)
├── llm.py                # Chamadas OpenRouter: identify_items() + rerank()
├── matcher.py            # Cascata alias → pg_trgm → LLM rerank
├── calculator.py         # Combina items LLM + matches → calcula totals
├── db.py                 # Pool asyncpg + helpers
├── schema.sql            # Schema Postgres (foods, food_aliases, meals)
├── import_taco.py        # Importa data/taco.json → foods
├── data/
│   └── taco.json         # TACO 4ª ed. (mirror de github.com/marcelosanto/tabela_taco)
├── requirements.txt
├── .env                  # Não commitado
└── README.md
```

---

## Variáveis de ambiente (`.env`)

```env
# Telegram
TELEGRAM_BOT_TOKEN=         # do BotFather
ALLOWED_USER_ID=            # opcional: trava o bot pro seu user_id; vazio = qualquer um

# OpenRouter
OPENROUTER_API_KEY=         # de openrouter.ai/keys
OPENROUTER_MODEL=google/gemini-2.5-flash             # vision LLM (configurável em runtime via /modelo)
OPENROUTER_RERANK_MODEL=google/gemma-4-31b-it    # modelo barato pra desempate

# Postgres (Supabase)
DATABASE_URL=postgresql://...
```

---

## Como o matching funciona (cascata)

Cada item identificado pela Vision LLM passa por uma cascata. O primeiro nível que resolver vence — os de baixo nem rodam.

### 1. `food_aliases` (custo: 0)
Lookup exato em uma tabela de aliases aprendidos. Se você já corrigiu "salmão na brasa" → "Peixe, salmão, grelhado" antes, esse mapeamento fica registrado e é usado direto.

### 2. `pg_trgm` + normalização (custo: 0)
Similaridade de trigramas entre `name_normalized` (lower + sem acentos, feito no Python) do alimento e dos 597 nomes da TACO. Threshold de aceitação: 0.55. Se top-1 passa com folga (gap >= 0.08 pro top-2), aceita direto. Performance: ~10ms via índice GIN.

### 3. LLM rerank (custo: ~$0.0001)
Quando os 5 melhores do trigram são ambíguos (ou top-1 < 0.55), o bot manda os candidatos + nome original pra um modelo barato (Gemma 4 31b por default) que escolhe o melhor match — ou retorna `null` se nenhum serve. Isso lida com casos tipo "salmão na brasa" (TACO tem "Peixe, salmão, fresco, grelhado" mas o trigram score fica baixo por causa da ordem das palavras).

### Sem match
Retorna marcador `❌ sem dados`. Pode ser corrigido com `✏️ trocar` se houver alternativas no top-5.

### Fallback estimativa (`🟡`)
Quando o item é processado/restaurante (sorvete industrial, refrigerante, marca específica), a Vision LLM marca `is_processed=true` e fornece os macros estimados direto — pula o matcher porque a TACO não vai ter essas entradas.

### Thresholds (em `matcher.py`)
```python
TRGM_THRESHOLD = 0.35       # mínimo absoluto pra considerar candidato
HIGH_CONF_THRESHOLD = 0.55  # acima disso, aceita sem rerank
GAP_FOR_TIEBREAK = 0.08     # se top1 - top2 < isso, considera empate e rerank
```

Ajustável conforme uso real. Se você ver muito "🟡 estimativa" indevido, abaixa `HIGH_CONF_THRESHOLD`. Se muito match errado direto, sobe.

---

## Aprendizado (food_aliases)

Cada vez que você usa o botão `✏️ trocar`:
1. Sua escolha vai pra `food_aliases (alias, food_id, user_id)`
2. Próxima foto, se a Vision LLM disser o mesmo nome → match direto, sem cascata.

Isso é o **moat** da abordagem: em 1-2 semanas de uso, o sistema fica calibrado pros pratos que **você** come.

---

## Custos

Por foto enviada:
- **Gemini 2.5 Flash** (visão): ~$0.003
- **Gemma 2 9B free** (rerank, quando precisa): $0
- **Telegram**: grátis
- **Supabase free tier**: 500MB, 60 conexões simultâneas — suficiente pra **anos** de uso individual
- **Total**: ~$0.10/mês pra uso típico (4 fotos/dia)

Pra zerar custo: usa `OPENROUTER_MODEL=google/gemini-2.5-flash:free` (rate-limit menor mas free). Ou outros modelos free com visão em [openrouter.ai/models?modality=text%2Bimage-%3Etext](https://openrouter.ai/models).

---

## Roadmap

### Curto prazo (next)
- [ ] **Vitat como fonte on-demand**: quando matcher falhar, busca em `vitat.com.br` (que tem receitas prontas e produtos industrializados que TACO não tem), cacheia em `foods` com `source='VITAT'`. Cresce o banco organicamente sem scraping em massa.
- [ ] **Ajuste de porção via botão**: `✏️ ajustar porção` que abre +/- 25g ou input livre.
- [ ] **Refeições recorrentes** (`/repetir cafe`): salva templates e reusa sem chamar LLM.
- [ ] **Timezone correto** (atualmente hardcoded BRT em [db.py:list_today](db.py)).

### Médio prazo
- [ ] **Comparativo entre modelos**: roda 2-3 Vision LLMs na mesma foto e mostra side-by-side pra você calibrar empiricamente qual prefere.
- [ ] **TBCA via scraping próprio** (cobre industrializados + receitas) — bloqueado por licença CC BY-NC-ND se virar produto.
- [ ] **Histórico/gráficos**: `/grafico semana` retorna PNG com tendência.
- [ ] **Targets**: define meta (ex: 2200 kcal/dia, 150g proteína) e mostra delta no `/hoje`.

### Longo prazo
- [ ] **Voice messages**: "tomei um café com leite e um pão de queijo" → STT → mesma cascata.
- [ ] **Multi-user com Supabase Auth**.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'dotenv'`
Venv não está ativo, ou `pip install -r requirements.txt` não rodou. Reativa:
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### `asyncpg.exceptions.DuplicatePreparedStatementError`
Você está usando o pgbouncer (porta 6543) sem desabilitar prepared statements. O código já desabilita (`statement_cache_size=0`) — confere se [db.py](db.py) está atualizado.

### `function unaccent(text) does not exist` / `gin_trgm_ops does not exist`
Extensão pg_trgm não instalada ou search_path não inclui `extensions`. Confere que `db.py:_init_conn` está executando `set search_path to public, extensions`.

### Bot não responde no Telegram
1. Confere que o terminal mostra `Bot rodando.`
2. Confere que está mandando mensagem pro bot certo (username que você criou no BotFather)
3. Se tiver `ALLOWED_USER_ID` configurado, confere que bate com seu user_id
4. Reinicia o bot (`Ctrl+C` + `python bot.py`)

### `OpenRouter 403 Forbidden`
- Modelo exige habilitar privacy/logging em [openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy)
- Sem créditos pro modelo pago
- Slug do modelo errado — confere em [openrouter.ai/models](https://openrouter.ai/models)

### Telegram retorna `Can't parse entities`
Algum caractere especial está quebrando o parser. O código usa HTML escape (`html.escape`) em todo conteúdo dinâmico, mas se aparecer de novo, é bug — abre issue com o stack trace.

---

## Licenças e fontes

- **Código**: este repo é seu — escolhe a licença que quiser.
- **TACO**: domínio quase-público; NEPA-UNICAMP exige citação ("NEPA-UNICAMP, TACO 4ª edição, 2011"). Uso pessoal/educacional OK. Comercial: confirma com a instituição.
- **JSON mirror**: [github.com/marcelosanto/tabela_taco](https://github.com/marcelosanto/tabela_taco).
