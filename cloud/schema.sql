-- Wafer cell database (PostgreSQL). Run: psql "$DATABASE_URL" -f schema.sql
create table if not exists wafer_events (
    id          bigserial primary key,
    cell        text        not null default 'wafer',
    wafer_id    text        not null,
    ts_start    timestamptz not null,
    ts_end      timestamptz not null,
    arm         text        not null,          -- arm_a / arm_b
    station     text        not null,          -- e.g. pickup / handoff / place
    cycle_s     real        not null,
    label       text,                          -- model class, e.g. ok / scratch / missing
    confidence  real,
    result      text        not null           -- good / defect
);
create index if not exists wafer_events_ts on wafer_events (ts_end desc);

create table if not exists oee_snapshots (
    id            bigserial primary key,
    cell          text        not null default 'wafer',
    ts            timestamptz not null,
    availability  real, performance real, quality real, oee real,
    run_s         integer, down_s integer,
    parts         integer, good integer, reject integer,
    running       boolean
);
create index if not exists oee_snapshots_ts on oee_snapshots (ts desc);

create table if not exists edge_status (
    cell        text primary key,
    online      boolean     not null,
    last_seen   timestamptz not null
);

create table if not exists alerts (
    id        bigserial primary key,
    cell      text        not null default 'wafer',
    ts        timestamptz not null default now(),
    kind      text        not null,            -- quality_low / stopped / edge_offline
    message   text        not null,
    cleared   timestamptz
);
create index if not exists alerts_open on alerts (cell, kind) where cleared is null;
