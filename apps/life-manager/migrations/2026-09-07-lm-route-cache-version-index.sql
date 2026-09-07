-- Additive follow-up to the already-applied route-cache failure migration. The event identity lets
-- the scheduler read a persisted positive or negative result before paid geocoding.
ALTER TABLE public.lm_route_cache
  ADD COLUMN IF NOT EXISTS event_version text,
  ADD COLUMN IF NOT EXISTS purpose text;

CREATE INDEX IF NOT EXISTS lm_route_cache_event_version_idx
  ON public.lm_route_cache (uid, event_version, purpose, computed_at DESC);

REVOKE ALL ON TABLE public.lm_route_cache FROM PUBLIC, anon, authenticated;
