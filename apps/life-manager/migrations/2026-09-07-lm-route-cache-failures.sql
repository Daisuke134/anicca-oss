-- COST-02: make the existing route cache usable by the production scoped cache key and allow
-- bounded negative entries. Additive and rerunnable; existing successful rows remain valid.
ALTER TABLE public.lm_route_cache
  ADD COLUMN IF NOT EXISTS cache_key text,
  ADD COLUMN IF NOT EXISTS cache_state text NOT NULL DEFAULT 'success',
  ADD COLUMN IF NOT EXISTS failure_class text;

ALTER TABLE public.lm_route_cache
  DROP CONSTRAINT IF EXISTS lm_route_cache_uid_from_geo_to_geo_time_bucket_key;

CREATE UNIQUE INDEX IF NOT EXISTS lm_route_cache_cache_key_idx
  ON public.lm_route_cache (cache_key);

ALTER TABLE public.lm_route_cache
  DROP CONSTRAINT IF EXISTS lm_route_cache_cache_state_check;
ALTER TABLE public.lm_route_cache
  ADD CONSTRAINT lm_route_cache_cache_state_check
  CHECK (cache_state IN ('success', 'negative'));

CREATE INDEX IF NOT EXISTS lm_route_cache_expiry_idx
  ON public.lm_route_cache (computed_at, ttl_secs);
