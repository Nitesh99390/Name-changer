-- =====================================================================
--  Novel Name Converter Bot - Supabase schema
--  Run this once in: Supabase dashboard -> SQL Editor -> New query
--
--  STORAGE POLICY (enforced by design, not only by convention):
--    * only METADATA is stored here;
--    * novel text, converted text and uploaded files are NEVER written to
--      the database or to Storage - the files live in /tmp on the server
--      for the duration of one job and are deleted immediately after.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Users
-- ---------------------------------------------------------------------
create table if not exists public.bot_users (
    user_id        bigint primary key,
    username       text,
    first_name     text,
    last_name      text,
    language_code  text,
    settings       jsonb not null default '{
        "latin_names": true,
        "notify_progress": true,
        "keep_original": false
    }'::jsonb,
    jobs_done      integer not null default 0,
    chars_total    bigint  not null default 0,
    names_total    bigint  not null default 0,
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 2. Conversion jobs (counters only - no file content, no file paths)
-- ---------------------------------------------------------------------
create table if not exists public.conversion_jobs (
    job_id             text primary key,
    user_id            bigint not null references public.bot_users(user_id) on delete cascade,
    chat_id            bigint,
    status             text not null default 'running'
                       check (status in ('running','done','failed','cancelled')),
    file_name          text,            -- original file NAME only, for the user's own history
    file_size          bigint not null default 0,
    char_count         integer not null default 0,
    replacement_count  integer not null default 0,
    duration_ms        integer not null default 0,
    mapping_version    text,
    error_code         text,
    created_at         timestamptz not null default now(),
    finished_at        timestamptz
);

create index if not exists conversion_jobs_user_time_idx
    on public.conversion_jobs (user_id, created_at desc);
create index if not exists conversion_jobs_status_idx
    on public.conversion_jobs (status);

-- ---------------------------------------------------------------------
-- 3. Global bot settings (key/value)
-- ---------------------------------------------------------------------
create table if not exists public.bot_settings (
    key        text primary key,
    value      jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 4. Mapping versions (which name mapping produced a given file)
-- ---------------------------------------------------------------------
create table if not exists public.name_mappings (
    version     text primary key,
    seed        text,
    pair_count  integer not null default 0,
    source      text,
    checksum    text,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 5. Atomic counter bump (called after a successful conversion)
-- ---------------------------------------------------------------------
create or replace function public.bump_user_counters(p_user_id bigint)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.bot_users
       set jobs_done   = jobs_done + 1,
           last_seen_at = now()
     where user_id = p_user_id;
end;
$$;

-- ---------------------------------------------------------------------
-- 6. Row Level Security
--    The bot talks to PostgREST with the SERVICE ROLE key (server side),
--    which bypasses RLS. RLS is enabled anyway so that a leaked anon key
--    still cannot read or write anything.
-- ---------------------------------------------------------------------
alter table public.bot_users       enable row level security;
alter table public.conversion_jobs enable row level security;
alter table public.bot_settings    enable row level security;
alter table public.name_mappings   enable row level security;

-- No policies are created on purpose => default deny for anon/authenticated.
-- Server-side service_role access is unaffected.

-- ---------------------------------------------------------------------
-- 7. Handy views for the Mini App / dashboard
-- ---------------------------------------------------------------------
create or replace view public.v_global_stats as
select
    (select count(*) from public.bot_users)                                     as users,
    (select count(*) from public.conversion_jobs)                               as jobs,
    (select count(*) from public.conversion_jobs where status = 'done')         as jobs_done,
    (select coalesce(sum(char_count), 0) from public.conversion_jobs
        where status = 'done')                                                  as chars_total,
    (select coalesce(sum(replacement_count), 0) from public.conversion_jobs
        where status = 'done')                                                  as names_total;

-- ---------------------------------------------------------------------
-- 8. Retention: keep the metadata table small on the free tier.
--    Optional - run from the Supabase scheduler if you want automatic
--    cleanup of job rows older than 90 days.
-- ---------------------------------------------------------------------
-- delete from public.conversion_jobs where created_at < now() - interval '90 days';
