-- L14: tenant-scoped, secret-free Investment Loop cutover state.
CREATE TABLE IF NOT EXISTS public.lm_investment_runtime_states (
  uid text PRIMARY KEY CHECK (uid ~ '^[A-Za-z0-9._-]{1,200}$'),
  bundle jsonb NOT NULL CHECK (jsonb_typeof(bundle) = 'object'),
  bundle_digest text NOT NULL CHECK (bundle_digest ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.lm_investment_runtime_states ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.lm_investment_runtime_states FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON TABLE public.lm_investment_runtime_states FROM anon';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'REVOKE ALL ON TABLE public.lm_investment_runtime_states FROM authenticated';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE public.lm_investment_runtime_states TO service_role';
  END IF;
END
$$;
