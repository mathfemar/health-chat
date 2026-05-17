-- Drop tudo e recria (MVP, sem migration)
drop table if exists messages cascade;
drop table if exists conversations cascade;
drop table if exists meals cascade;
drop table if exists food_portions cascade;
drop table if exists food_aliases cascade;
drop table if exists foods cascade;

create extension if not exists pg_trgm;

-- ============ ALIMENTOS ============
create table foods (
    id              serial primary key,
    source          text not null,             -- 'TACO' | 'VITAT' | 'USER'
    source_id       int,
    name            text not null,
    name_normalized text not null,
    category        text,
    kcal            numeric(7,2) not null,     -- por 100g
    protein_g       numeric(6,2) not null,
    carbs_g         numeric(6,2) not null,
    fat_g           numeric(6,2) not null,
    fiber_g         numeric(6,2),
    created_at      timestamptz default now(),
    unique (source, source_id)
);

create index foods_name_trgm
  on foods using gin (name_normalized gin_trgm_ops);

-- Porções nomeadas (Vitat traz: "filé pequeno" = 170 kcal, etc).
create table food_portions (
    id        serial primary key,
    food_id   int not null references foods(id) on delete cascade,
    name      text not null,            -- "filé pequeno", "1 unidade", "concha"
    name_normalized text not null,
    grams     numeric(7,2),             -- pode ser null se não conhecido
    kcal      numeric(7,2) not null,
    protein_g numeric(6,2) not null,
    carbs_g   numeric(6,2) not null,
    fat_g     numeric(6,2) not null
);
create index food_portions_food_idx on food_portions(food_id);

-- Aliases aprendidos: "salmao na brasa" -> food_id
create table food_aliases (
    alias       text primary key,
    food_id     int not null references foods(id) on delete cascade,
    user_id     bigint,
    created_at  timestamptz not null default now()
);

-- ============ REFEIÇÕES ============
create table meals (
    id            bigserial primary key,
    user_id       bigint not null,
    created_at    timestamptz not null default now(),
    eaten_at      timestamptz not null default now(),
    vision_model  text,
    photo_file_id text,
    items         jsonb not null,
    kcal          numeric(8,2) not null,
    protein_g     numeric(7,2) not null,
    carbs_g       numeric(7,2) not null,
    fat_g         numeric(7,2) not null,
    notes         text
);
create index meals_user_eaten_idx on meals (user_id, eaten_at desc);

-- ============ CONVERSAS ============
create table conversations (
    id         bigserial primary key,
    user_id    bigint not null,
    started_at timestamptz not null default now(),
    last_at    timestamptz not null default now(),
    state      text default 'active',         -- 'active' | 'closed'
    summary    text,                          -- resumo das msgs antigas
    -- scratchpad: estado leve do agente entre turnos
    -- ex: {"last_menu_id": 7, "last_dish_chosen": "Picanha"}
    scratchpad jsonb default '{}'::jsonb
);
create index conversations_user_idx on conversations(user_id, last_at desc);

create table messages (
    id              bigserial primary key,
    conversation_id bigint not null references conversations(id) on delete cascade,
    role            text not null,             -- 'user' | 'assistant' | 'tool' | 'system'
    content         text,                      -- texto (user/assistant); JSON do resultado (tool)
    tool_calls      jsonb,                     -- [{id, name, args}] quando assistant chama tools
    tool_call_id    text,                      -- quando role='tool', amarra ao call
    photo_file_id   text,                      -- imagem anexada (Telegram file_id)
    created_at      timestamptz not null default now()
);
create index messages_conv_idx on messages(conversation_id, created_at);

-- ============ PERFIL DO USUÁRIO ============
create table user_profiles (
    user_id      bigint primary key,
    name         text,
    daily_kcal   int,
    daily_protein_g int,
    preferences  text,                         -- texto livre: "evito glúten, gosto de carne vermelha"
    updated_at   timestamptz default now()
);
