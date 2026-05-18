-- Drop tudo e recria (MVP, sem migration)
drop table if exists weight_log cascade;
drop table if exists exercises cascade;
drop table if exists messages cascade;
drop table if exists conversations cascade;
drop table if exists meals cascade;
drop table if exists food_portions cascade;
drop table if exists food_aliases cascade;
drop table if exists foods cascade;
drop table if exists user_profiles cascade;

create extension if not exists pg_trgm;

-- ============ ALIMENTOS ============
create table foods (
    id              serial primary key,
    source          text not null,
    source_id       int,
    name            text not null,
    name_normalized text not null,
    category        text,
    kcal            numeric(7,2) not null,
    protein_g       numeric(6,2) not null,
    carbs_g         numeric(6,2) not null,
    fat_g           numeric(6,2) not null,
    fiber_g         numeric(6,2),
    created_at      timestamptz default now(),
    unique (source, source_id)
);
create index foods_name_trgm on foods using gin (name_normalized gin_trgm_ops);

create table food_portions (
    id              serial primary key,
    food_id         int not null references foods(id) on delete cascade,
    name            text not null,
    name_normalized text not null,
    grams           numeric(7,2),
    kcal            numeric(7,2) not null,
    protein_g       numeric(6,2) not null,
    carbs_g         numeric(6,2) not null,
    fat_g           numeric(6,2) not null
);
create index food_portions_food_idx on food_portions(food_id);

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

-- ============ EXERCÍCIOS ============
create table exercises (
    id            bigserial primary key,
    user_id       bigint not null,
    created_at    timestamptz not null default now(),
    done_at       timestamptz not null default now(),
    activity      text not null,
    duration_min  int,
    kcal_burned   int not null,
    avg_hr        int,
    distance_km   numeric(5,2),
    source        text not null,            -- 'watch_photo' | 'manual' | 'agent'
    photo_file_id text,
    notes         text,
    raw_ocr       jsonb
);
create index exercises_user_done_idx on exercises (user_id, done_at desc);

-- ============ CONVERSAS ============
create table conversations (
    id         bigserial primary key,
    user_id    bigint not null,
    started_at timestamptz not null default now(),
    last_at    timestamptz not null default now(),
    state      text default 'active',
    summary    text,
    scratchpad jsonb default '{}'::jsonb
);
create index conversations_user_idx on conversations(user_id, last_at desc);

create table messages (
    id              bigserial primary key,
    conversation_id bigint not null references conversations(id) on delete cascade,
    role            text not null,
    content         text,
    tool_calls      jsonb,
    tool_call_id    text,
    photo_file_id   text,
    created_at      timestamptz not null default now()
);
create index messages_conv_idx on messages(conversation_id, created_at);

-- ============ PERFIL DO USUÁRIO ============
create table user_profiles (
    user_id           bigint primary key,
    name              text,
    sex               text,                    -- 'M' | 'F' | 'O'
    birth_date        date,
    height_cm         int,
    current_weight_kg numeric(5,2),
    target_weight_kg  numeric(5,2),
    activity_level    text,                    -- 'sedentary'|'light'|'moderate'|'active'|'very_active'
    weekly_rate_kg    numeric(3,2),            -- negativo=perder, positivo=ganhar
    eatback_pct       int default 100,         -- 0..100 — quanto das kcal queimadas vão pro budget
    daily_kcal        int,                     -- calculado e cacheado
    daily_protein_g   int,                     -- calculado e cacheado
    preferences       text,
    -- Lembrete de pesagem
    weigh_in_enabled  boolean default true,
    weigh_in_hour     int default 6,                       -- hora local (0-23)
    weigh_in_tz       text default 'America/Sao_Paulo',
    weigh_in_last_date date,                               -- última vez que enviamos hoje
    updated_at        timestamptz default now()
);

-- Histórico de peso (pra gráfico de tendência)
create table weight_log (
    id          bigserial primary key,
    user_id     bigint not null,
    weight_kg   numeric(5,2) not null,
    measured_at timestamptz default now(),
    source      text default 'manual'        -- 'manual' | 'photo'
);
create index weight_log_user_idx on weight_log(user_id, measured_at desc);
