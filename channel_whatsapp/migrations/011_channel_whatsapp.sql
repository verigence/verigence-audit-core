-- =====================================================================
-- MIGRATION 011 — channel.whatsapp tables
--
-- Applied after the base Verigence schema (migrations 001-010).
-- All tenant-scoped tables carry RLS with FORCE row level security.
-- wa.inbox and wa.contact deliberately have NO RLS — see comments below.
-- =====================================================================

-- WhatsApp routes: one row per registered business number.
create table if not exists wa.route (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references iam.tenant(id) on delete cascade,
  phone_number_id text unique not null,
  display_number  text not null,
  active          boolean not null default true,
  created_at      timestamptz not null default now()
);
create index wa_route_active on wa.route (phone_number_id) where active;

create table if not exists wa.binding_code (
  code_hash  text primary key,
  user_id    uuid not null references iam.app_user(id) on delete cascade,
  tenant_id  uuid not null references iam.tenant(id) on delete cascade,
  phone_e164 text not null,
  expires_at timestamptz not null,
  used_at    timestamptz,
  created_at timestamptz not null default now()
);

-- NO RLS: webhook writes before identity is resolved.
create table if not exists wa.inbox (
  id              bigserial primary key,
  wamid           text unique,
  phone_number_id text,
  received_at     timestamptz not null default now(),
  payload         jsonb not null,
  signature_ok    boolean not null,
  state           text not null default 'pending'
                  check (state in ('pending','processing','done','failed','ignored')),
  attempts        integer not null default 0,
  last_error      text,
  locked_until    timestamptz
);
create index wa_inbox_claim on wa.inbox (state, id) where state = 'pending';

-- NO RLS: resolution begins with phone and DISCOVERS the tenant.
create table if not exists wa.contact (
  id           uuid primary key default gen_random_uuid(),
  phone_e164   text unique not null,
  wa_id        text,
  user_id      uuid not null references iam.app_user(id) on delete cascade,
  tenant_id    uuid not null references iam.tenant(id) on delete cascade,
  status       text not null default 'pending'
               check (status in ('pending','active','suspended','revoked')),
  locale       text not null default 'en'
               check (locale in ('en','hi','pa')),
  verified_at  timestamptz,
  last_seen_at timestamptz,
  last_deal_id uuid references doc.deal(id) on delete set null,
  last_deal_at timestamptz,
  created_at   timestamptz not null default now()
);
create index wa_contact_tenant on wa.contact (tenant_id);

create table if not exists wa.session (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references iam.tenant(id) on delete cascade,
  contact_id   uuid not null references wa.contact(id) on delete cascade,
  deal_id      uuid references doc.deal(id) on delete set null,
  org_unit_id  uuid references iam.org_unit(id),
  state        text not null default 'collecting'
               check (state in ('collecting','confirming_deal','processing',
                                'gaps_pending','complete','parked','escalated','cancelled')),
  note         text,
  flush_at     timestamptz,
  expires_at   timestamptz,
  file_count   integer not null default 0,
  bytes_total  bigint not null default 0,
  created_at   timestamptz not null default now(),
  submitted_at timestamptz,
  completed_at timestamptz
);
create index wa_session_flush on wa.session (flush_at)
  where state in ('collecting','confirming_deal','gaps_pending');
create unique index wa_session_one_open_per_contact
  on wa.session (contact_id)
  where state in ('collecting','confirming_deal','processing');

create table if not exists wa.file (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references iam.tenant(id) on delete cascade,
  session_id       uuid not null references wa.session(id) on delete cascade,
  wamid            text unique not null,
  media_id         text not null,
  received_seq     integer not null,
  wa_timestamp     timestamptz not null,
  kind             text not null check (kind in ('document','image')),
  fidelity         text not null check (fidelity in ('original','recompressed')),
  declared_mime    text,
  declared_name    text,
  caption          text,
  meta_sha256      text,
  local_sha256     text,
  byte_size        bigint,
  page_count       integer,
  state            text not null default 'pending'
                   check (state in ('pending','downloading','redacting','classifying',
                                    'storing','stored','failed','skipped','quarantined')),
  attempts         integer not null default 0,
  last_error       text,
  error_code       text,
  storage_uri      text,
  media_expires_at timestamptz,
  created_at       timestamptz not null default now(),
  stored_at        timestamptz
);
create index wa_file_session on wa.file (session_id);
create index wa_file_expiry  on wa.file (media_expires_at) where state <> 'stored';

create table if not exists wa.outbox (
  id         bigserial primary key,
  tenant_id  uuid not null references iam.tenant(id) on delete cascade,
  contact_id uuid not null references wa.contact(id) on delete cascade,
  session_id uuid references wa.session(id) on delete set null,
  kind       text not null check (kind in ('text','interactive','list','audio','template')),
  payload    jsonb not null,
  state      text not null default 'pending'
             check (state in ('pending','sent','failed','skipped_window')),
  attempts   integer not null default 0,
  send_after timestamptz not null default now(),
  sent_at    timestamptz,
  wamid      text,
  last_error text
);
create index wa_outbox_claim on wa.outbox (state, send_after) where state = 'pending';

alter table wa.session enable row level security;
alter table wa.session force  row level security;
alter table wa.file    enable row level security;
alter table wa.file    force  row level security;
alter table wa.outbox  enable row level security;
alter table wa.outbox  force  row level security;

create policy wa_tenant_isolation on wa.session
  using (tenant_id = current_setting('app.tenant_id', true)::uuid);
create policy wa_tenant_isolation on wa.file
  using (tenant_id = current_setting('app.tenant_id', true)::uuid);
create policy wa_tenant_isolation on wa.outbox
  using (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- revoke all on wa.inbox, wa.contact from app_web;
-- grant  all on wa.inbox, wa.contact to   app_worker;

create or replace view wa.contact_scoped as
  select * from wa.contact
  where tenant_id = current_setting('app.tenant_id', true)::uuid;
