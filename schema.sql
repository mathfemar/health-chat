-- Drop old, recreate from scratch (MVP cedo, nada de migration)
drop table if exists meals cascade;
drop table if exists food_aliases cascade;
drop table if exists foods cascade;

create extension if not exists pg_trgm;

create table foods (
    id              serial primary key,
    source          text   not null,            -- 'TACO', 'TBCA', 'USER'
    source_id       int,                        -- id na tabela original
    name            text   not null,            -- "Arroz, integral, cozido"
    name_normalized text   not null,            -- lower + sem acentos (feito em Python)
    category        text,
    kcal            numeric(7,2) not null,      -- por 100g
    protein_g       numeric(6,2) not null,
    carbs_g         numeric(6,2) not null,
    fat_g           numeric(6,2) not null,
    fiber_g         numeric(6,2),
    unique (source, source_id)
);

create index foods_name_trgm
  on foods using gin (name_normalized gin_trgm_ops);

create table food_aliases (
    alias       text primary key,            -- normalizado (lower + unaccent)
    food_id     int not null references foods(id) on delete cascade,
    user_id     bigint,                      -- quem aprendeu (null = global)
    created_at  timestamptz not null default now()
);

create table meals (
    id            bigserial primary key,
    user_id       bigint      not null,
    created_at    timestamptz not null default now(),
    eaten_at      timestamptz not null default now(),
    vision_model  text        not null,
    photo_file_id text,
    -- items: [{name_llm, food_id, food_name, portion_g, kcal, p, c, f, source, match_score}]
    items         jsonb       not null,
    kcal          numeric(8,2) not null,
    protein_g     numeric(7,2) not null,
    carbs_g       numeric(7,2) not null,
    fat_g         numeric(7,2) not null,
    notes         text
);

create index meals_user_eaten_idx on meals (user_id, eaten_at desc);
