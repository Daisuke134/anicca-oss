CREATE TABLE IF NOT EXISTS public.lm_financial_transition_receipts (
  subject_id text NOT NULL,
  event_key text NOT NULL CHECK (char_length(event_key) BETWEEN 1 AND 256),
  record_id text NOT NULL CHECK (record_id ~ '^financial:[0-9a-f]{64}$'),
  status text NOT NULL CHECK (status IN ('pending', 'sent')),
  telegram_message_id bigint CHECK (telegram_message_id IS NULL OR telegram_message_id > 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  sent_at timestamptz,
  PRIMARY KEY (subject_id, event_key),
  CHECK (
    (status = 'sent' AND telegram_message_id IS NOT NULL AND sent_at IS NOT NULL)
    OR (status = 'pending' AND telegram_message_id IS NULL AND sent_at IS NULL)
  )
);

ALTER TABLE public.lm_financial_transition_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.lm_financial_transition_receipts FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.lm_financial_transition_receipts TO service_role;

DROP POLICY IF EXISTS lm_financial_transition_service_select ON public.lm_financial_transition_receipts;
CREATE POLICY lm_financial_transition_service_select
  ON public.lm_financial_transition_receipts FOR SELECT TO service_role USING (true);
DROP POLICY IF EXISTS lm_financial_transition_service_insert ON public.lm_financial_transition_receipts;
CREATE POLICY lm_financial_transition_service_insert
  ON public.lm_financial_transition_receipts FOR INSERT TO service_role WITH CHECK (true);
DROP POLICY IF EXISTS lm_financial_transition_service_update ON public.lm_financial_transition_receipts;
CREATE POLICY lm_financial_transition_service_update
  ON public.lm_financial_transition_receipts FOR UPDATE TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS lm_financial_transition_service_delete ON public.lm_financial_transition_receipts;
CREATE POLICY lm_financial_transition_service_delete
  ON public.lm_financial_transition_receipts FOR DELETE TO service_role USING (true);
