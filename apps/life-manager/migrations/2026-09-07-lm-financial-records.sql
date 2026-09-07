-- Immutable common FinancialRecord persistence for cloud runtimes.
CREATE TABLE IF NOT EXISTS public.lm_financial_records (
  record_id text NOT NULL CHECK (char_length(record_id) BETWEEN 1 AND 128),
  subject_id text NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
  idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 1024),
  occurred_at timestamptz NOT NULL,
  record jsonb NOT NULL CHECK (
    jsonb_typeof(record) = 'object'
    AND record->>'record_type' = 'financial_record'
    AND record->>'record_id' = record_id
    AND record->>'subject_id' = subject_id
    AND record->>'idempotency_key' = idempotency_key
    AND (record->>'occurred_at')::timestamptz = occurred_at
    AND octet_length(record::text) <= 16384
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (subject_id, record_id),
  UNIQUE (subject_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS lm_financial_records_subject_time_idx
  ON public.lm_financial_records (subject_id, occurred_at, record_id);

ALTER TABLE public.lm_financial_records ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.lm_financial_records FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON TABLE public.lm_financial_records TO service_role;

DROP POLICY IF EXISTS lm_financial_records_service_select ON public.lm_financial_records;
CREATE POLICY lm_financial_records_service_select ON public.lm_financial_records
  FOR SELECT TO service_role USING (true);
DROP POLICY IF EXISTS lm_financial_records_service_insert ON public.lm_financial_records;
CREATE POLICY lm_financial_records_service_insert ON public.lm_financial_records
  FOR INSERT TO service_role WITH CHECK (true);

CREATE OR REPLACE FUNCTION public.reject_lm_financial_record_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  RAISE EXCEPTION 'financial records are immutable';
END
$$;

DROP TRIGGER IF EXISTS lm_financial_records_immutable ON public.lm_financial_records;
CREATE TRIGGER lm_financial_records_immutable
BEFORE UPDATE OR DELETE ON public.lm_financial_records
FOR EACH ROW EXECUTE FUNCTION public.reject_lm_financial_record_mutation();
