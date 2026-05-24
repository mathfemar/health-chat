# health-chat

Bot de Telegram conversacional pra registrar **alimentação + exercício + peso** com base nutricional brasileira real (TACO + Vitat) — em vez de só "chutômetro" de LLM.

> **Por que existe**: MyFitnessPal/Cal AI/Lifesum têm churn alto porque exigem abrir um app à parte e digitar tudo. Aqui o registro acontece dentro do messenger que você já abre o dia inteiro. **Foto, texto ou áudio → bot resolve → diário atualizado, gráfico bonito, meta recalculada.**

---

## Diferenciais

| | Cal AI / MFP / Lifesum | health-chat |
|---|---|---|
| Onde roda | App próprio (mais um app pra abrir) | **Dentro do Telegram** |
| Base nutricional | Caixa-preta ou genérica USA | **TACO (UNICAMP) + Vitat brasileira** |
| Foto de prato | LLM chuta macros (erro ~30%) | LLM **identifica**, banco **calcula** |
| Foto de cardápio | Não existe | **Sim** — parseia + ranqueia por meta |
| Foto de relógio fitness | Não existe | **Sim** — extrai treino do Apple Watch / Garmin / Strava |
| Foto de balança | Não existe | **Sim** — lê peso, recalcula meta |
| Aprendizado | Não aprende | Cada correção vira **alias permanente** |
| Custo/mês | R\$ 35-50 | **~R\$ 0,50** (~$0,10) |

---

## Funcionalidades

### Conversação com agente (Gemma 4 31B ou DeepSeek V4 via OpenRouter)
- Mensagem livre → agente decide quais **tools** chamar
- Foto → identifica se é prato, cardápio, relógio ou balança e roteia
- Histórico de conversa persistido em Postgres (20 últimas msgs em janela)
- Tool calling nativo (não ReAct — usa o protocolo OpenAI)

### Comida
- **Vision LLM identifica** alimentos + porção em gramas (e medida caseira quando aplicável)
- **Matcher em cascata**: alias aprendido → pg_trgm local → LLM rerank (Gemma free)
- **Base nutricional**: TACO 4ª ed. (597 alimentos brasileiros) + cache crescente da Vitat
- **Vitat on-demand**: quando TACO não tem (estrogonofe, sushi, marcas, etc), busca no [vitat.com.br](https://vitat.com.br), cacheia local em `foods (source='VITAT')` — chamada online só uma vez por alimento

### Exercício
- **Foto do relógio** (Apple Watch, Garmin, Strava, Polar, Whoop): extrai atividade, duração, kcal queimadas, distância, BPM médio
- **Manual via texto**: "fiz 1h de corrida, 580 kcal"
- **Tabela `exercises`** com fonte (`watch_photo` | `manual` | `agent`)

### Peso e metas
- **Mifflin-St Jeor + fatores de atividade NEAT** (conservadores, sem inflar como MFP)
- **Auto-recálculo** da meta toda vez que `log_weight` for chamado
- **Foto da balança** → extrai número → loga + recalcula meta
- **Lembrete diário de pesagem** via JobQueue (default 6h, configurável)
- **Histórico de peso** com gráfico de média móvel 7d + linha da meta
- **Eat-back configurável** (100% MFP / 50% Noom / 0% ignora)

### Visualização
- **Gráfico diário** (matplotlib): anel de kcal + barras de macros + breakdown por refeição — auto-anexado após `log_meal` e via `/grafico`
- **Gráfico semanal/mensal**: intake vs queimado vs meta por dia
- **Gráfico de peso**: pontos diários + MM 7d + meta horizontal

---

## Arquitetura

```
mensagem do user (texto, foto, ou ambos)
        │
        ▼
┌─────────────────────────────────────────────┐
│            bot.py (Telegram)               │
│  comandos diretos OU rota pro agente       │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│       agent/runtime.py — loop de tool      │
│       calling com Gemma 4 31B              │
│  • carrega últimas 20 msgs                 │
│  • monta payload (system + tools schema)   │
│  • executa tools que o modelo escolher     │
│  • máx 8 tool calls/turno + wrap-up        │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│     agent/tools.py — 24 tools              │
│  • search_foods, search_vitat,             │
│    fetch_vitat_food, get_food_portions     │
│  • estimate_meal_from_photo, parse_menu,   │
│    parse_watch_photo, parse_scale_photo    │
│  • log_meal, log_exercise, log_weight      │
│  • set_profile, compute_daily_goal,        │
│    compare_to_goal                         │
│  • get_today_summary, get_recent_meals,    │
│    get_calorie_balance, get_period_summary │
│  • generate_daily_chart,                   │
│    generate_weight_chart,                  │
│    generate_report_chart                   │
│  • remember (scratchpad da conversa)       │
└─────────────────────────────────────────────┘
        │            │              │
        ▼            ▼              ▼
   matcher.py    calculator.py   sources/vitat.py
   (cascata     (portion × per_  (busca online,
   pg_trgm)     100g macros)     bug ×100 fix)
        │            │              │
        └────────────┼──────────────┘
                     ▼
            Postgres (Supabase):
            foods | food_portions | food_aliases
            meals | exercises | weight_log
            conversations | messages | user_profiles
```

---

## Stack

| Camada | Tecnologia |
|---|---|
| Bot Telegram | [python-telegram-bot 21.x](https://github.com/python-telegram-bot/python-telegram-bot) (com `[ext]` pra JobQueue) |
| HTTP | httpx async |
| Vision LLM | Gemini 3 Flash Preview via OpenRouter (configurável) |
| Chat LLM (agente) | Gemma 4 31B ou DeepSeek V4 Flash via OpenRouter (configurável) |
| Rerank LLM | Gemma 4 31B ou outro free |
| Banco | Postgres no Supabase (free tier) |
| Matching textual | `pg_trgm` (similaridade de trigramas) |
| Cálculos | `goals.py` (Mifflin-St Jeor, deterministico) |
| Gráficos | matplotlib (Agg, sem GUI) |
| Base nutricional | TACO 4ª ed. (NEPA/UNICAMP) + Vitat on-demand |

---

## Setup do zero

### Pré-requisitos
- Python 3.11+ (testado em 3.13)
- Conta no [Telegram](https://t.me/BotFather)
- Conta no [Supabase](https://supabase.com) (free tier)
- Conta no [OpenRouter](https://openrouter.ai) (com $2-5 de crédito)

### 1. Bot do Telegram
1. Abre [@BotFather](https://t.me/BotFather)
2. `/newbot` → segue as instruções → guarda o **token**
3. (Opcional) `/setdescription` e `/setuserpic`

### 2. Supabase
1. Cria projeto novo em [supabase.com](https://supabase.com)
2. Escolhe região **South America (São Paulo)**
3. **Anota a senha do Postgres** (só aparece uma vez)
4. Botão verde **Connect** (canto superior direito)
5. Aba **Connection string** → **URI** → modo **Transaction pooler** (porta 6543) OU **Direct connection** (5432)
6. Substitui `[YOUR-PASSWORD]` pela senha do passo 3

### 3. OpenRouter
1. Cria conta em [openrouter.ai](https://openrouter.ai)
2. **Settings → Privacy**: habilita "Enable training and logging" (alguns modelos free exigem)
3. **Keys**: gera uma nova → guarda
4. **Credits**: adiciona $2-5 (Gemini Flash custa ~$0.003/foto; agente ~$0.002/turno)

### 4. Código

```powershell
git clone https://github.com/mathfemar/health-chat.git
cd health-chat

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Configura
copy .env.example .env
notepad .env       # preenche os campos abaixo

# Cria schema + popula 590 alimentos do TACO
python import_taco.py
```

---

## Como rodar

Você tem **3 modos** de execução. Use o que se encaixar no que você quer testar.

### Modo 1 — Só Telegram (mais simples)

```powershell
python bot.py
```

Requer `TELEGRAM_BOT_TOKEN` no `.env`. Se as envs do Twilio estiverem **vazias**, o adapter WhatsApp não sobe e o bot roda só no Telegram.

No Telegram, manda `/start` pro seu bot e depois `"quero definir minha meta"` pra começar o onboarding.

### Modo 2 — Só WhatsApp (standalone)

Útil pra testar o WhatsApp isoladamente, ou rodar sem precisar de bot Telegram.

```powershell
python -m uvicorn whatsapp:app --host 0.0.0.0 --port 8000
```

Requer as 5 envs do Twilio no `.env` (ver seção [WhatsApp (Twilio)](#whatsapp-twilio--opcional-roda-em-paralelo-ao-telegram) abaixo). Não precisa de `TELEGRAM_BOT_TOKEN`.

> **Dica de debug**: pra subir o servidor antes de ter o túnel pronto, põe `TWILIO_VALIDATE=0` no `.env` (desabilita validação de assinatura). Depois muda pra `1`.

### Modo 3 — Telegram + WhatsApp juntos (produção)

```powershell
python bot.py
```

Quando as envs do Twilio estão **preenchidas**, o `bot.py` sobe automaticamente também o servidor FastAPI do WhatsApp em paralelo (mesma process). Os dois canais compartilham banco/histórico/perfil.

Você vai ver nos logs:
```
Telegram polling iniciado.
WhatsApp adapter iniciando em 0.0.0.0:8000
```

### Pré-requisito comum: túnel pra Twilio (modos 2 e 3)

Twilio precisa alcançar seu PC via URL pública. Em **outra janela do PowerShell**:

```powershell
# Instala (uma vez só)
winget install --id Cloudflare.cloudflared

# Sobe o túnel apontando pra porta 8000
cloudflared tunnel --url http://localhost:8000
```

Copia a URL `https://xxx-yyy.trycloudflare.com` que aparecer, cola em `PUBLIC_BASE_URL` no `.env`, e no Twilio Console (**Messaging → Sandbox Settings → When a message comes in**) cola `<URL>/twilio/webhook` POST.

> A URL muda toda vez que você reinicia o `cloudflared`. Pra rodar 24/7, criar um [túnel nomeado](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (grátis, URL fixa).

### Parando o bot

`Ctrl+C` na janela do bot/uvicorn. O cloudflared é separado, também `Ctrl+C` na janela dele.

---

## Variáveis de ambiente (`.env`)

```env
# Telegram
TELEGRAM_BOT_TOKEN=                        # do BotFather

# Trava o bot pro seu user_id (vazio = qualquer um pode usar)
ALLOWED_USER_ID=

# OpenRouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemini-3-flash-preview            # vision LLM
OPENROUTER_CHAT_MODEL=google/gemma-4-31b-it               # agente conversacional
OPENROUTER_RERANK_MODEL=google/gemma-2-9b-it:free         # desempate de matching (opcional)

# Postgres (Supabase)
DATABASE_URL=postgresql://postgres.xxx:SENHA@aws-0-sa-east-1.pooler.supabase.com:6543/postgres
```

---

## WhatsApp (Twilio) — opcional, roda em paralelo ao Telegram

O bot continua falando Telegram normalmente. Se você preencher as creds do Twilio no `.env`, sobe **também** um servidor FastAPI que recebe mensagens via webhook do Twilio — mesmo agente, mesmo banco, mesmo histórico (no modo single-user, seu telefone é mapeado pro seu `ALLOWED_USER_ID` do Telegram).

### 1. Cria conta Twilio
1. [twilio.com/try-twilio](https://www.twilio.com/try-twilio) — conta grátis ($15 de trial).
2. No console (canto superior direito): **Account SID** e **Auth Token**. Copia.

### 2. Ativa o WhatsApp sandbox (rápido, sem aprovação)
1. Console Twilio → **Messaging → Try it out → Send a WhatsApp message**.
2. Mostra o número do sandbox (`+1 415 523 8886`) e um código `join xxxxx`.
3. No seu WhatsApp, manda `join xxxxx` pra esse número. Pronto, seu telefone está ligado ao sandbox.
4. `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886` no `.env`.

> **Limitação do sandbox**: depois de 24h sem você mandar mensagem, o Twilio só deixa o bot te responder se você mandar `join xxxxx` de novo. Pra produção (sem essa limitação), tem que comprar um número e passar pela aprovação do WhatsApp Business.

### 3. Expõe o servidor pra internet

Seu PC em casa não tem IP público, então precisa de um túnel. Recomendo **Cloudflare Tunnel** (grátis, estável, melhor que ngrok pra rodar 24/7):

```powershell
# Instala cloudflared (Windows)
winget install --id Cloudflare.cloudflared

# Sobe um túnel apontando pra porta 8000 (sem precisar de conta)
cloudflared tunnel --url http://localhost:8000
```

A saída mostra algo tipo `https://xxx-yyy.trycloudflare.com`. Copia.

> **Alternativa**: `ngrok http 8000` se preferir.

### 4. Preenche o `.env`

```env
TWILIO_ACCOUNT_SID=ACxxxxx
TWILIO_AUTH_TOKEN=xxxxx
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
WHATSAPP_LINK_PHONE=+5511987654321        # seu telefone (E.164)
PUBLIC_BASE_URL=https://xxx-yyy.trycloudflare.com
```

### 5. Configura o webhook no Twilio
Console Twilio → **Messaging → Settings → WhatsApp sandbox settings**:
- "When a message comes in" → `https://xxx-yyy.trycloudflare.com/twilio/webhook` (POST)
- Salva.

### 6. Sobe o bot
```powershell
python bot.py
```
Logs vão mostrar `Telegram polling iniciado` **e** `WhatsApp adapter iniciando em 0.0.0.0:8000`. Manda uma mensagem no WhatsApp pro número do sandbox — deve responder igual ao Telegram, com o mesmo histórico de conversa.

### Como mídia funciona
- **Foto recebida**: Twilio manda `MediaUrl0` na webhook → adapter baixa via basic auth → manda pra Vision LLM exatamente como faz no Telegram.
- **Foto enviada** (gráficos): adapter guarda em cache em memória e expõe em `GET /media/<token>` — Twilio busca e entrega no WhatsApp. Cache expira em 10min.

### Lembrete de pesagem nos dois canais
Com WhatsApp habilitado, o lembrete diário é enviado **tanto no Telegram quanto no WhatsApp** pro mesmo usuário. Em produção (fora do sandbox), conversas iniciadas pelo bot fora da janela de 24h exigem **template aprovado** no WhatsApp Business — se o lembrete começar a falhar, é provavelmente isso.

### Segurança
Por default o adapter valida `X-Twilio-Signature` em todo POST (descarta requests forjados). Se estiver debugando sem URL pública, pode setar `TWILIO_VALIDATE=0` temporariamente.

---

## Comandos diretos

| Comando | O que faz |
|---|---|
| `/start` ou `/ajuda` | Mostra todos os comandos |
| `/perfil` | Vê seu perfil completo + meta calórica |
| `/hoje` | Refeições + totais do dia |
| `/semana` | Total dos últimos 7 dias |
| `/grafico` | **Gráfico bonito do dia** (anel kcal + macros + refeições) |
| `/relatorio [semana\|mes\|N]` | Gráfico de intake vs queimado vs meta no período |
| `/lembrete [off\|on\|0-23]` | Configura lembrete diário de pesagem (default 6h) |
| `/buscar <termo>` | Busca alimento no banco local (TACO + Vitat cacheado) |
| `/apagar` | Remove a última refeição |
| `/reset` | Começa nova conversa com o agente (zera histórico) |

---

## Conversação com o agente (exemplos)

### Onboarding
```
você:  oi
bot:   Olá! Vamos montar seu perfil. Qual seu sexo biológico?
       Responda M / F / O.
você:  M
bot:   Qual sua data de nascimento? Formato YYYY-MM-DD.
... (continua até 8 perguntas, depois calcula meta)
```

### Logar refeição
```
você:  comi 100g de arroz, 150g de bolo de carne e 50g de purê de abóbora
bot:   ✅ Refeição #12 logada: 510 kcal (28g proteína).
       [gráfico do dia anexado]
```

### Foto de cardápio
```
você:  [foto do cardápio]  quero algo com carne, faltam 800 kcal
bot:   Vi 4 opções com carne vermelha:
       1. Picanha grelhada (~580 kcal, ~55g prot) ⭐ melhor pra você
       2. Bife à parmegiana (~720 kcal)
       3. Strogonoff (~640 kcal)
       Qual escolheu?
```

### Foto do relógio (Apple Watch / Strava)
```
você:  [foto do treino]
bot:   Identifiquei: corrida, 45min, 412 kcal queimadas, 7.2 km, FC média 154.
       Loga?
você:  sim
bot:   ✅ Exercício registrado.
```

### Foto da balança
```
você:  [foto da balança]
bot:   Vejo 101.4 kg. Confirma?
você:  sim
bot:   ✅ Peso 101.4 kg registrado. Nova meta calórica: 1852 kcal/dia (Δ -10).
```

### Texto livre
```
você:  101.8       # número solto = vai direto pra log_weight
bot:   ✅ Peso 101.8 kg registrado. Nova meta: 1858 kcal/dia.

você:  como tá meu dia?
bot:   Você comeu 1420 kcal, queimou 380 no treino. Com eat-back 0%, ainda pode comer 432 kcal pra fechar a meta de 1852.
```

---

## Estrutura de arquivos

```
health-chat/
├── bot.py                     # Handlers Telegram + JobQueue (lembrete)
├── llm.py                     # Chamadas OpenRouter: vision, menu, scale, watch, rerank
├── matcher.py                 # Cascata alias → pg_trgm → LLM rerank
├── calculator.py              # portion × per_100g; recalc com override
├── db.py                      # Pool asyncpg + helpers
├── goals.py                   # Mifflin-St Jeor + fatores NEAT
├── schema.sql                 # 9 tabelas: foods, meals, exercises, conversations, etc.
├── import_taco.py             # Carrega data/taco.json → foods (idempotente)
├── agent/
│   ├── prompts.py             # SYSTEM_PROMPT + prompts de visão (food/menu/watch/scale)
│   ├── schemas.py             # TypedDicts (Food, Meal, Portion, etc)
│   ├── registry.py            # @tool decorator + sanitização de args
│   ├── tools.py               # 24 tools que o agente pode chamar
│   ├── runtime.py             # Loop tool-calling + fallback inteligente
│   └── charts.py              # matplotlib (daily_progress, weight_trend, period)
├── sources/
│   └── vitat.py               # Cliente Vitat (buildId + search + fetch, bug ×100 fix)
├── data/
│   └── taco.json              # TACO 4ª ed.
├── tests/
│   └── test_vitat.py          # E2E do cliente Vitat (10 termos)
├── slides/                    # Apresentação Beamer (LaTeX)
│   └── apresentacao.tex
├── requirements.txt
├── .env.example
├── .env                       # não commitado
└── README.md
```

---

## Como o matching de alimentos funciona

Cada nome de alimento (vindo da Vision LLM ou do usuário) passa por uma cascata. Primeiro nível que resolver vence — os de baixo nem rodam.

### Nível 1: `food_aliases` (custo $0)
Lookup exato em tabela de aliases aprendidos. Se você já corrigiu "salmão na brasa" → "Peixe, salmão, fresco, grelhado" antes, esse mapping fica registrado e é usado direto.

### Nível 2: `pg_trgm` + normalização Python (custo $0)
Similaridade de trigramas entre `name_normalized` (lower + sem acentos) do alimento e dos ~600 nomes da TACO + Vitat cacheado.
- Threshold de aceitação: 0.55 com gap >= 0.08 pro 2º colocado
- Performance: ~10ms via índice GIN
- Mesmo com erro de digitação ou palavra extra, normalmente acerta

### Nível 3: `search_vitat` + `fetch_vitat_food` (custo $0 — só latência)
Se o local não bate (score < 0.3), agente busca online no Vitat. Pega o melhor hit, baixa detalhes, salva em `foods (source='VITAT')`. **Próxima vez, cai no Nível 2 direto** — Vitat é chamada uma vez por alimento.

### Nível 4: LLM rerank (custo ~$0.0001)
Quando trigrama é ambíguo (ex: "strogonoff de frango" não tem match óbvio), agente passa top-5 candidatos pra um modelo barato (Gemma) que escolhe — ou diz "nenhum representa".

### Fallback: estimativa LLM (marcador 🟡)
Industrializados/restaurantes que nem Vitat tem: a Vision LLM marca `is_processed=true` e fornece macros estimados. Calculator usa esses números, marca o item como 🟡.

---

## Cálculo de meta (Mifflin-St Jeor com fatores NEAT)

### BMR (Mifflin-St Jeor, 1990)
```
BMR (M) = 10×peso + 6,25×altura − 5×idade + 5
BMR (F) = 10×peso + 6,25×altura − 5×idade − 161
```
Padrão clínico moderno, ±5% de erro contra calorimetria indireta. Referência: [Mifflin et al, AJCN 1990](https://pubmed.ncbi.nlm.nih.gov/2305711/).

### Fatores de atividade — APENAS NEAT (sem treino)
```
sedentary    1,20  — mesa o dia inteiro
light        1,30  — anda no escritório/casa
moderate     1,40  — trabalho com circulação (professor, garçom)
active       1,50  — trabalho braçal (construção, entregador)
very_active  1,65  — trabalho fisicamente muito demandante
```

**Importante**: estes fatores são conservadores comparados aos do MFP/Cal AI (que usa até 1,9). Os multiplicadores antigos vinham de pesquisa dos anos 90 que superestima NEAT. A literatura recente com água duplamente marcada confirma a faixa 1.2-1.65 pra adultos modernos sem treino estruturado.

**Treino é registrado separadamente** via `log_exercise` e somado ao budget conforme o `eatback_pct` (100% MFP / 50% Noom / 0% ignora).

### Meta diária
```
TDEE = BMR × fator_atividade
déficit = ritmo_kg_sem × 7700 / 7      (negativo pra perder)
meta = max(piso_seguro, TDEE + déficit)
```
Pisos: 1500 kcal (M) / 1200 kcal (F).

### Macros sugeridos
- **Proteína**: 1,6 g/kg corporal (faixa de preservação muscular)
- **Carbo / Gordura**: split 50/50 do restante após proteína

---

## Custos típicos

| Operação | Modelo | Custo |
|---|---|---|
| Foto de prato (Vision + matcher + log) | Gemini 3 Flash | ~$0,005 |
| Turno conversacional simples | Gemma 4 31B | ~$0,002 |
| Foto de cardápio + ranking | Gemini + 2-3 tools | ~$0,01 |
| Foto de relógio + log | Gemini + 1 tool | ~$0,003 |
| Foto de balança + log + recalc | Gemini + 2 tools | ~$0,003 |
| /grafico (sem LLM) | matplotlib local | $0 |
| Vitat search/fetch (rede) | – | $0 |

**Uso típico (4 fotos + 5 chats / dia)**: ~$0,10/mês.
**Free tier total**: dá pra rodar 100% grátis trocando pra modelos `:free` no `.env` (DeepSeek free, Gemma 2 free, Gemini 2.5 Flash free) — com rate limits menores.

---

## Roadmap

### Curto prazo
- [ ] Comparativo entre Vision LLMs side-by-side
- [ ] Refeições recorrentes (`/repetir cafe`)
- [ ] Botão de ajuste de porção pós-log
- [ ] Multi-foto numa msg só (rótulo + prato)

### Médio prazo
- [ ] Voice messages (STT via Whisper)
- [ ] Sugestões proativas ("ainda faltam 50g de proteína hoje")
- [ ] Integração com HealthKit/Google Fit
- [ ] TBCA via scraping pessoal

### Longo prazo
- [ ] Multi-user com auth (Supabase Auth)
- [x] Versão WhatsApp via Twilio (roda em paralelo ao Telegram)
- [ ] Dashboard web (Next.js consumindo os mesmos endpoints)

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'dotenv'`
Venv não ativado, ou `pip install -r requirements.txt` não rodou.

### `asyncpg.exceptions.DuplicatePreparedStatementError`
Pgbouncer (porta 6543) não aceita prepared statements. O código já desabilita (`statement_cache_size=0`) — confirma que [db.py](db.py) está atualizado.

### `function unaccent / gin_trgm_ops does not exist`
Extensão pg_trgm não no search_path. Confere [db.py:_init_conn](db.py) executando `set search_path to public, extensions`.

### `OpenRouter 401 "User not found"`
Key revogada/inválida. Vai em [openrouter.ai/keys](https://openrouter.ai/keys), gera nova, cola no `.env`.

### `OpenRouter 403`
Modelo exige privacy/logging em [openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy), ou sem créditos, ou slug errado.

### Telegram `409 Conflict: terminated by other getUpdates`
Outro processo do bot rodando com o mesmo token. Mata um.

### Telegram `Can't parse entities`
HTML mal-formado da LLM. O código sanitiza em `bot._md_to_html` + `_balance_html_tags`. Se acontecer, abre issue com o stack trace.

### Agente alucinando nome / reinicia onboarding
DeepSeek V4 Flash tem essa fraqueza. Troca pra Gemma 4 31B no `.env` (`OPENROUTER_CHAT_MODEL=google/gemma-4-31b-it`) e reinicia.

### Vitat retornando 0 hits / `buildId` não extraído
Vitat mudou layout. Confere [sources/vitat.py:BUILD_ID_PROBE_PATHS](sources/vitat.py) e roda `python tests/test_vitat.py` pra debugar.

---

## Licenças e fontes

- **Código**: este repo é seu — escolhe a licença que quiser.
- **TACO**: uso pessoal/educacional livre com citação ("NEPA-UNICAMP, TACO 4ª edição, 2011"). Comercial: confirma com a instituição.
- **TACO JSON**: mirror de [github.com/marcelosanto/tabela_taco](https://github.com/marcelosanto/tabela_taco).
- **Vitat**: dados são públicos; uso pessoal/individual via endpoints `_next/data`. Não fazemos scraping em massa — apenas on-demand quando o usuário busca um alimento específico.
