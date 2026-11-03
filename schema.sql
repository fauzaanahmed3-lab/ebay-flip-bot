-- One table for everything the scanner flags. item_id is unique so nothing is alerted twice.
create table if not exists public.flip_listings (
  item_id          text primary key,
  category         text not null,
  variant          text,
  title            text,
  url              text,
  image_url        text,
  price            numeric,
  shipping         numeric,
  all_in           numeric,
  buy_target       numeric,
  expected_sell    numeric,
  profit           numeric,
  required_profit  int,
  verdict          text,          -- DEAL or OFFER
  suggested_offer  int,
  seller           text,
  seller_feedback  int,
  seller_pct       numeric,
  flags            text,
  listed_at        timestamptz,
  first_seen       timestamptz not null default now(),
  status           text not null default 'new',   -- new / main / extra / bought / gone / rejected
  review_note      text,          -- Claude's note from the 10am review
  lane             text,          -- EXACT / GEM / UNCONFIRMED (or VAGUE for rejects)
  rating           text,          -- HOT / SOLID / CHECK
  notes            text           -- plain-English notes shown in Discord
);

create index if not exists flip_listings_first_seen_idx on public.flip_listings (first_seen desc);

-- Lock the table down: only the server-side key (used by the scanner) can read/write.
alter table public.flip_listings enable row level security;
