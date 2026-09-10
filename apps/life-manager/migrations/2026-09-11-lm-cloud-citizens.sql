CREATE SCHEMA IF NOT EXISTS private;
REVOKE ALL ON SCHEMA private FROM PUBLIC, anon, authenticated;

CREATE TABLE IF NOT EXISTS public.lm_cloud_citizens (
  tenant_id text PRIMARY KEY,
  citizen_id text NOT NULL UNIQUE,
  instance_id text NOT NULL UNIQUE,
  wallet_address text NOT NULL UNIQUE CHECK (wallet_address ~ '^0x[0-9a-f]{40}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id),
  UNIQUE (tenant_id, citizen_id, instance_id)
);

CREATE TABLE IF NOT EXISTS private.lm_cloud_citizen_signers (
  tenant_id text PRIMARY KEY,
  citizen_id text NOT NULL,
  instance_id text NOT NULL,
  ciphertext bytea NOT NULL,
  iv bytea NOT NULL CHECK (octet_length(iv) = 12),
  tag bytea NOT NULL CHECK (octet_length(tag) = 16),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id, citizen_id, instance_id),
  FOREIGN KEY (tenant_id, citizen_id, instance_id)
    REFERENCES public.lm_cloud_citizens(tenant_id, citizen_id, instance_id) ON DELETE CASCADE
);

ALTER TABLE public.lm_cloud_citizens ENABLE ROW LEVEL SECURITY;
ALTER TABLE private.lm_cloud_citizen_signers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.lm_cloud_citizens FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE private.lm_cloud_citizen_signers FROM PUBLIC, anon, authenticated;
GRANT SELECT ON TABLE public.lm_cloud_citizens TO service_role;
GRANT USAGE ON SCHEMA private TO service_role;
GRANT SELECT ON TABLE private.lm_cloud_citizen_signers TO service_role;

CREATE OR REPLACE FUNCTION public.provision_lm_cloud_citizen(
  p_tenant_id text,
  p_citizen_id text,
  p_instance_id text,
  p_wallet_address text,
  p_ciphertext bytea,
  p_iv bytea,
  p_tag bytea
) RETURNS TABLE (
  tenant_id text,
  citizen_id text,
  instance_id text,
  wallet_address text,
  created boolean
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, private AS $$
DECLARE
  existing public.lm_cloud_citizens%ROWTYPE;
BEGIN
  IF p_tenant_id IS NULL OR p_tenant_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR p_citizen_id IS NULL OR p_instance_id IS NULL
     OR p_wallet_address !~ '^0x[0-9a-f]{40}$'
     OR octet_length(p_iv) <> 12 OR octet_length(p_tag) <> 16
     OR octet_length(p_ciphertext) < 1 THEN
    RAISE EXCEPTION 'cloud citizen input invalid';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id, 8453));
  SELECT * INTO existing FROM public.lm_cloud_citizens c WHERE c.tenant_id = p_tenant_id;
  IF FOUND THEN
    RETURN QUERY SELECT existing.tenant_id, existing.citizen_id, existing.instance_id,
      existing.wallet_address, false;
    RETURN;
  END IF;

  INSERT INTO public.lm_cloud_citizens AS c (tenant_id, citizen_id, instance_id, wallet_address)
  VALUES (p_tenant_id, p_citizen_id, p_instance_id, lower(p_wallet_address))
  RETURNING c.* INTO existing;
  INSERT INTO private.lm_cloud_citizen_signers
    (tenant_id, citizen_id, instance_id, ciphertext, iv, tag)
  VALUES (p_tenant_id, p_citizen_id, p_instance_id, p_ciphertext, p_iv, p_tag);
  RETURN QUERY SELECT existing.tenant_id, existing.citizen_id, existing.instance_id,
    existing.wallet_address, true;
END;
$$;

REVOKE ALL ON FUNCTION public.provision_lm_cloud_citizen(text,text,text,text,bytea,bytea,bytea)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.provision_lm_cloud_citizen(text,text,text,text,bytea,bytea,bytea)
  TO service_role;

CREATE OR REPLACE FUNCTION public.complete_lm_runtime_job_and_enqueue(
  p_tenant_id text, p_job_id text, p_attempt integer, p_worker_id text, p_receipt jsonb,
  p_next_job_id text, p_next_loop_id text, p_next_capability text,
  p_next_effect_class text, p_next_effect_key text, p_next_tenant_id text,
  p_next_input_refs jsonb, p_next_max_attempts integer, p_next_available_at timestamptz
) RETURNS SETOF public.lm_runtime_job_receipts
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  completed public.lm_runtime_job_receipts%ROWTYPE;
  queued public.lm_runtime_jobs%ROWTYPE;
BEGIN
  IF p_next_tenant_id <> p_tenant_id THEN RAISE EXCEPTION 'runtime continuation tenant mismatch'; END IF;
  SELECT * INTO completed FROM public.complete_lm_runtime_job(
    p_tenant_id, p_job_id, p_attempt, p_worker_id, p_receipt
  );
  IF NOT FOUND THEN RAISE EXCEPTION 'runtime completion lost lease'; END IF;

  INSERT INTO public.lm_runtime_jobs (
    job_id, tenant_id, loop_id, capability, effect_class, effect_key,
    input_refs, max_attempts, available_at
  ) VALUES (
    p_next_job_id, p_next_tenant_id, p_next_loop_id, p_next_capability,
    p_next_effect_class, p_next_effect_key, p_next_input_refs,
    p_next_max_attempts, p_next_available_at
  ) ON CONFLICT (job_id) DO NOTHING;

  SELECT * INTO queued FROM public.lm_runtime_jobs
  WHERE job_id = p_next_job_id AND tenant_id = p_next_tenant_id;
  IF NOT FOUND
     OR queued.loop_id IS DISTINCT FROM p_next_loop_id
     OR queued.capability IS DISTINCT FROM p_next_capability
     OR queued.effect_class IS DISTINCT FROM p_next_effect_class
     OR queued.effect_key IS DISTINCT FROM p_next_effect_key
     OR queued.input_refs IS DISTINCT FROM p_next_input_refs
     OR queued.max_attempts IS DISTINCT FROM p_next_max_attempts
     OR queued.available_at IS DISTINCT FROM p_next_available_at THEN
    RAISE EXCEPTION 'runtime continuation collision';
  END IF;
  RETURN NEXT completed;
END;
$$;

REVOKE ALL ON FUNCTION public.complete_lm_runtime_job_and_enqueue(
  text,text,integer,text,jsonb,text,text,text,text,text,text,jsonb,integer,timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.complete_lm_runtime_job_and_enqueue(
  text,text,integer,text,jsonb,text,text,text,text,text,text,jsonb,integer,timestamptz
) TO service_role;
