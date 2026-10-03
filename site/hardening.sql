-- SUPERSEDED by audit_fixes.sql. Do not run this file.
-- Hardening for the shareholder lookup (run once in the Supabase SQL editor of project `shareholder-lookup`).
-- Adds: lockout after repeated wrong codes or repeated "not found" probing, per-visitor and global daily caps,
-- a 5 s time limit per lookup. Safe to re-run.

-- 0. remove the 200 test rows that were loaded twice
delete from sl_private.items where id <= 200;

create table if not exists sl_private.events (ip text not null, kind text not null, t timestamptz not null default now());
create index if not exists events_idx on sl_private.events (ip, kind, t);
alter table sl_private.events enable row level security;
revoke all on sl_private.events from public, anon, authenticated;

insert into sl_private.settings (key, value) values ('ip_daily_cap','300'), ('global_daily_cap','3000')
on conflict (key) do nothing;

create or replace function sl_private.client_ip() returns text
language sql stable set search_path = '' as $$
  select coalesce(nullif(split_part(coalesce(current_setting('request.headers', true)::json ->> 'x-forwarded-for', ''), ',', 1), ''), 'unknown')
$$;

drop function if exists sl_private.gate(text);
create or replace function sl_private.gate(p_code text) returns text
language plpgsql security definer set search_path = '' as $$
declare v_ip text := sl_private.client_ip(); v_n int; v_req text; v_hash text; v_cap int;
begin
  if random() < 0.02 then
    delete from sl_private.hits where t < now() - interval '2 days';
    delete from sl_private.events where t < now() - interval '2 days';
  end if;
  select count(*) into v_n from sl_private.events where ip = v_ip and kind = 'fail' and t > now() - interval '15 minutes';
  if v_n >= 5 then return 'locked'; end if;
  select count(*) into v_n from sl_private.events where ip = v_ip and kind = 'miss' and t > now() - interval '1 hour';
  if v_n >= 15 then return 'locked'; end if;
  select count(*) into v_n from sl_private.hits where ip = v_ip and t > now() - interval '1 minute';
  if v_n >= 20 then return 'rate_limited'; end if;
  select count(*) into v_n from sl_private.hits where ip = v_ip and t > now() - interval '1 day';
  select value::int into v_cap from sl_private.settings where key = 'ip_daily_cap';
  if v_n >= coalesce(v_cap, 300) then return 'daily_cap'; end if;
  select count(*) into v_n from sl_private.hits where t > now() - interval '1 day';
  select value::int into v_cap from sl_private.settings where key = 'global_daily_cap';
  if v_n >= coalesce(v_cap, 3000) then return 'daily_cap'; end if;
  insert into sl_private.hits (ip) values (v_ip);
  select value into v_req from sl_private.settings where key = 'require_code';
  if v_req = 'true' then
    select value into v_hash from sl_private.settings where key = 'code_hash';
    if p_code is null or encode(extensions.digest(lower(btrim(p_code)), 'sha256'), 'hex') <> v_hash then
      insert into sl_private.events (ip, kind) values (v_ip, 'fail');
      return 'code_required';
    end if;
  end if;
  return 'ok';
end $$;

create or replace function public.ss_lookup_bo(p_bo text, p_code text default null) returns jsonb
language plpgsql security definer set search_path = '' set statement_timeout = '5s' as $$
declare v_bo text := regexp_replace(coalesce(p_bo,''), '[^0-9]', '', 'g'); v_ids bigint[]; v_status text; v_res jsonb;
begin
  v_status := sl_private.gate(p_code);
  if v_status <> 'ok' then return jsonb_build_object('error', v_status); end if;
  if length(v_bo) <> 16 then return jsonb_build_object('error', 'invalid_bo'); end if;
  select array_agg(id) into v_ids from sl_private.items where bo_id = v_bo;
  v_res := sl_private.result_for(coalesce(v_ids, '{}'::bigint[]));
  if not (v_res ->> 'found')::boolean then
    insert into sl_private.events (ip, kind) values (sl_private.client_ip(), 'miss');
  end if;
  return v_res;
end $$;

create or replace function public.ss_lookup_folio(p_company text, p_folio text, p_code text default null) returns jsonb
language plpgsql security definer set search_path = '' set statement_timeout = '5s' as $$
declare v_key text := upper(regexp_replace(coalesce(p_folio,''), '[\s-]', '', 'g')); v_ids bigint[]; v_status text; v_res jsonb;
begin
  v_status := sl_private.gate(p_code);
  if v_status <> 'ok' then return jsonb_build_object('error', v_status); end if;
  if length(v_key) < 1 or length(v_key) > 30 or coalesce(p_company,'') = '' then return jsonb_build_object('error', 'invalid_folio'); end if;
  select array_agg(id) into v_ids from sl_private.items where company = p_company and folio_key = v_key;
  v_res := sl_private.result_for(coalesce(v_ids, '{}'::bigint[]));
  if not (v_res ->> 'found')::boolean then
    insert into sl_private.events (ip, kind) values (sl_private.client_ip(), 'miss');
  end if;
  return v_res;
end $$;

revoke execute on function public.ss_lookup_bo(text, text), public.ss_lookup_folio(text, text, text) from public;
grant execute on function public.ss_lookup_bo(text, text), public.ss_lookup_folio(text, text, text) to anon, authenticated;

-- 1. TURN ON THE ACCESS CODE: replace <SHA256_OF_LOWERCASE_CODE> with the hash of your code, then run these two lines.
--    (hash from a terminal:  printf '%s' 'your-code' | tr A-Z a-z | sha256sum)
-- update sl_private.settings set value = '<SHA256_OF_LOWERCASE_CODE>' where key = 'code_hash';
-- update sl_private.settings set value = 'true' where key = 'require_code';
--
-- Rotate the code any time by running the first line again with a new hash. Turn it off with require_code = 'false'.
-- Tighten or loosen limits:  update sl_private.settings set value = '150' where key = 'ip_daily_cap';
