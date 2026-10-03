-- Audit fixes for the shareholder lookup (project `shareholder-lookup`), 2026-10-03.
-- Supersedes hardening.sql (which was never applied). Safe to re-run.
-- Run it in the Supabase dashboard: SQL Editor -> New query -> paste this file -> Run.
--
-- 1. Rate limiter keyed on the real client IP. X-Forwarded-For is passed through from the client
--    unchanged, so anyone could send a fake one and get a fresh quota per request. cf-connecting-ip
--    is set by Cloudflare in front of Supabase and cannot be spoofed.
-- 2. Lockout after repeated wrong access codes or repeated "not found" probing, per-IP and global
--    daily caps, 5 s limit per lookup. Limits live in sl_private.settings so they can be tuned.
-- 3. Data: remove the 200 test rows that were loaded twice (ids 1-200 are exact copies of 201-400);
--    mark negative amounts as needing a check so they are not added into totals.
-- 4. Lookups always use the partial indexes; results carry a needs-check count; ss_info carries
--    coverage figures for the landing page.

-- 3. data ------------------------------------------------------------------------------------------
delete from sl_private.items a
using sl_private.items b
where a.id <= 200 and b.id = a.id + 200
  and (a.bo_id, a.company, a.folio_key, a.folio_no, a.year, a.year_start, a.dtype, a.net, a.shares, a.name, a.doc_id, a.page, a.verified, a.flags)
      is not distinct from
      (b.bo_id, b.company, b.folio_key, b.folio_no, b.year, b.year_start, b.dtype, b.net, b.shares, b.name, b.doc_id, b.page, b.verified, b.flags);

update sl_private.items
set verified = false,
    flags = concat_ws(';', nullif(flags, ''), 'negative_amount')
where net < 0 and coalesce(flags, '') not like '%negative_amount%';

-- 2. tables and settings ---------------------------------------------------------------------------
create table if not exists sl_private.events (ip text not null, kind text not null, t timestamptz not null default now());
create index if not exists events_idx on sl_private.events (ip, kind, t);
alter table sl_private.events enable row level security;
revoke all on sl_private.events from public, anon, authenticated;

insert into sl_private.settings (key, value) values
  ('ip_daily_cap', '300'), ('global_daily_cap', '5000'), ('per_minute_cap', '20'),
  ('fail_limit_15m', '5'), ('miss_limit_1h', '30')
on conflict (key) do nothing;

-- 1. client ip -------------------------------------------------------------------------------------
create or replace function sl_private.client_ip() returns text
language sql stable set search_path = '' as $$
  select coalesce(
    nullif(btrim(current_setting('request.headers', true)::json ->> 'cf-connecting-ip'), ''),
    nullif(btrim(split_part(current_setting('request.headers', true)::json ->> 'x-forwarded-for', ',', 1)), ''),
    'unknown')
$$;

create or replace function sl_private.setting_int(p_key text, p_default int) returns int
language sql stable set search_path = '' as $$
  select coalesce((select nullif(value, '')::int from sl_private.settings where key = p_key), p_default)
$$;

-- gate_check: returns 'ok' or an error code the page knows how to explain. (The older
-- sl_private.gate() raised exceptions instead; it is left in place but no longer called.)
create or replace function sl_private.gate_check(p_code text) returns text
language plpgsql security definer set search_path = '' as $$
declare v_ip text := sl_private.client_ip(); v_n int; v_req text; v_hash text;
begin
  if random() < 0.02 then
    delete from sl_private.hits where t < now() - interval '2 days';
    delete from sl_private.events where t < now() - interval '2 days';
  end if;
  select count(*) into v_n from sl_private.events where ip = v_ip and kind = 'fail' and t > now() - interval '15 minutes';
  if v_n >= sl_private.setting_int('fail_limit_15m', 5) then return 'locked'; end if;
  select count(*) into v_n from sl_private.events where ip = v_ip and kind = 'miss' and t > now() - interval '1 hour';
  if v_n >= sl_private.setting_int('miss_limit_1h', 30) then return 'locked'; end if;
  select count(*) into v_n from sl_private.hits where ip = v_ip and t > now() - interval '1 minute';
  if v_n >= sl_private.setting_int('per_minute_cap', 20) then return 'rate_limited'; end if;
  select count(*) into v_n from sl_private.hits where ip = v_ip and t > now() - interval '1 day';
  if v_n >= sl_private.setting_int('ip_daily_cap', 300) then return 'daily_cap'; end if;
  select count(*) into v_n from sl_private.hits where t > now() - interval '1 day';
  if v_n >= sl_private.setting_int('global_daily_cap', 5000) then return 'daily_cap'; end if;
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
revoke all on function sl_private.gate_check(text) from public;

-- 4. results ---------------------------------------------------------------------------------------
create or replace function sl_private.result_for(p_ids bigint[]) returns jsonb
language sql stable security definer set search_path = '' as $$
  with it as (
    select i.*, d.title as doc_title, d.source_url, d.status as doc_status
    from sl_private.items i left join sl_private.docs d on d.doc_id = i.doc_id
    where i.id = any(p_ids)
  ), comp as (
    select company,
      coalesce(sum(net) filter (where verified and coalesce(dtype,'') not in ('stock','right')), 0) as cash,
      coalesce(sum(shares) filter (where verified and dtype in ('stock','right')), 0) as shares,
      count(*) as n,
      count(*) filter (where not verified) as unverified,
      jsonb_agg(jsonb_build_object(
        'year', year, 'type', dtype, 'net', net, 'shares', shares, 'verified', verified,
        'doc', doc_id, 'doc_title', doc_title, 'page', page, 'url', source_url,
        'doc_status', doc_status, 'folio', folio_no
      ) order by year_start nulls last, year) as items
    from it group by company
  )
  select jsonb_build_object(
    'found', exists (select 1 from it),
    'name', (select sl_private.mask_name(name) from it where coalesce(name,'') <> '' group by name order by count(*) desc limit 1),
    'totals', jsonb_build_object('cash', coalesce(sum(cash),0), 'shares', coalesce(sum(shares),0), 'companies', count(*),
                                 'items', coalesce(sum(n),0), 'unverified', coalesce(sum(unverified),0)),
    'companies', coalesce(jsonb_agg(jsonb_build_object('company', company, 'cash', cash, 'shares', shares, 'count', n,
                                                       'unverified', unverified, 'items', items) order by cash desc, shares desc), '[]'::jsonb)
  ) from comp
$$;

create or replace function public.ss_lookup_bo(p_bo text, p_code text default null) returns jsonb
language plpgsql security definer set search_path = '' set statement_timeout = '5s' as $$
declare v_bo text := regexp_replace(left(coalesce(p_bo,''), 64), '[^0-9]', '', 'g'); v_ids bigint[]; v_status text; v_res jsonb;
begin
  v_status := sl_private.gate_check(p_code);
  if v_status <> 'ok' then return jsonb_build_object('error', v_status); end if;
  if length(v_bo) <> 16 then return jsonb_build_object('error', 'invalid_bo'); end if;
  select array_agg(id) into v_ids from sl_private.items where bo_id <> '' and bo_id = v_bo;
  v_res := sl_private.result_for(coalesce(v_ids, '{}'::bigint[]));
  if not (v_res ->> 'found')::boolean then
    insert into sl_private.events (ip, kind) values (sl_private.client_ip(), 'miss');
  end if;
  return v_res;
end $$;

create or replace function public.ss_lookup_folio(p_company text, p_folio text, p_code text default null) returns jsonb
language plpgsql security definer set search_path = '' set statement_timeout = '5s' as $$
declare v_key text := upper(regexp_replace(left(coalesce(p_folio,''), 64), '[\s-]', '', 'g')); v_ids bigint[]; v_status text; v_res jsonb;
begin
  v_status := sl_private.gate_check(p_code);
  if v_status <> 'ok' then return jsonb_build_object('error', v_status); end if;
  if length(v_key) < 1 or length(v_key) > 30 or coalesce(p_company,'') = '' then return jsonb_build_object('error', 'invalid_folio'); end if;
  select array_agg(id) into v_ids from sl_private.items where folio_key <> '' and company = p_company and folio_key = v_key;
  v_res := sl_private.result_for(coalesce(v_ids, '{}'::bigint[]));
  if not (v_res ->> 'found')::boolean then
    insert into sl_private.events (ip, kind) values (sl_private.client_ip(), 'miss');
  end if;
  return v_res;
end $$;

-- coverage figures, computed once here (re-run this block after every data load)
insert into sl_private.settings (key, value)
select 'stats', jsonb_build_object(
  'companies', (select count(distinct company) from sl_private.items),
  'documents', (select count(*) from sl_private.docs),
  'records',   (select count(*) from sl_private.items),
  'cash',      (select round(coalesce(sum(net), 0)) from sl_private.items where verified and coalesce(dtype,'') not in ('stock','right')),
  'shares',    (select round(coalesce(sum(shares), 0)) from sl_private.items where verified and dtype in ('stock','right'))
)::text
on conflict (key) do update set value = excluded.value;

create or replace function public.ss_info() returns jsonb
language sql stable security definer set search_path = '' as $$
  select jsonb_build_object(
    'updated', (select value from sl_private.settings where key = 'data_updated'),
    'require_code', coalesce((select value = 'true' from sl_private.settings where key = 'require_code'), false),
    'stats', (select value::jsonb from sl_private.settings where key = 'stats'))
$$;

revoke execute on function public.ss_lookup_bo(text, text), public.ss_lookup_folio(text, text, text), public.ss_info(), public.ss_companies() from public;
grant execute on function public.ss_lookup_bo(text, text), public.ss_lookup_folio(text, text, text), public.ss_info(), public.ss_companies() to anon, authenticated;

-- ACCESS CODE (optional): replace <SHA256_OF_LOWERCASE_CODE>, then run both lines.
--   hash from a terminal:  printf '%s' 'your-code' | tr A-Z a-z | sha256sum
-- update sl_private.settings set value = '<SHA256_OF_LOWERCASE_CODE>' where key = 'code_hash';
-- update sl_private.settings set value = 'true' where key = 'require_code';
-- Tune limits, e.g. before a demo from one office network:
-- update sl_private.settings set value = '60' where key = 'miss_limit_1h';
