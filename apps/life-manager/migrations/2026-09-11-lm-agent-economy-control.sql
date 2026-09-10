ALTER TABLE public.lm_cloud_citizens
  ADD COLUMN IF NOT EXISTS agent_economy_paused_at timestamptz;
