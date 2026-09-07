-- Monthly customer allowance. One tenant/event key consumes one unit only after user value succeeds.
-- Pending reservations close concurrency races and expire after 15 minutes if a worker disappears.

CREATE TABLE IF NOT EXISTS public.lm_managed_action_ledger (
  uid text NOT NULL,
  period_start date NOT NULL,
  action_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'succeeded')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  PRIMARY KEY (uid, period_start, action_key)
);

ALTER TABLE public.lm_managed_action_ledger ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS public.lm_managed_allowance_notice (
  uid text NOT NULL,
  period_start date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (uid, period_start)
);

ALTER TABLE public.lm_managed_allowance_notice ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.lm_managed_allowance_result(
  p_uid text,
  p_period_start date,
  p_action_key text,
  p_allowed boolean
) RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
  SELECT jsonb_build_object(
    'allowed', p_allowed,
    'used', count(*) FILTER (WHERE l.status = 'succeeded'),
    'limit', CASE WHEN COALESCE(u.paid, false) THEN 500 ELSE 30 END,
    'periodStart', p_period_start::text,
    'resetAt', (p_period_start + INTERVAL '1 month')::date::text,
    'actionKey', p_action_key
  )
  FROM public.lm_users AS u
  LEFT JOIN public.lm_managed_action_ledger AS l
    ON l.uid = u.uid AND l.period_start = p_period_start
  WHERE u.uid = p_uid
  GROUP BY u.paid;
$function$;

CREATE OR REPLACE FUNCTION public.reserve_lm_managed_action(
  p_uid text,
  p_action_key text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  period date := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;
  cap integer;
  active_count integer;
  existing_status text;
  allowed boolean := false;
  notify boolean := false;
BEGIN
  IF p_uid IS NULL OR btrim(p_uid) = '' OR char_length(p_uid) > 256
     OR p_action_key IS NULL OR btrim(p_action_key) = '' OR char_length(p_action_key) > 512 THEN
    RAISE EXCEPTION 'invalid managed action identity';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_uid || ':' || period::text, 0));
  SELECT CASE WHEN paid THEN 500 ELSE 30 END INTO cap FROM public.lm_users WHERE uid = p_uid;
  IF cap IS NULL THEN RAISE EXCEPTION 'unknown tenant'; END IF;
  SELECT status INTO existing_status FROM public.lm_managed_action_ledger
    WHERE uid = p_uid AND period_start = period AND action_key = p_action_key;
  IF existing_status IS NOT NULL THEN
    allowed := true;
  ELSE
    DELETE FROM public.lm_managed_action_ledger
      WHERE uid = p_uid AND period_start = period AND status = 'pending'
        AND created_at < clock_timestamp() - INTERVAL '15 minutes';
    SELECT count(*) INTO active_count FROM public.lm_managed_action_ledger
      WHERE uid = p_uid AND period_start = period;
    IF active_count < cap THEN
      INSERT INTO public.lm_managed_action_ledger(uid, period_start, action_key, status)
      VALUES (p_uid, period, p_action_key, 'pending');
      allowed := true;
    ELSE
      INSERT INTO public.lm_managed_allowance_notice(uid, period_start)
      VALUES (p_uid, period) ON CONFLICT DO NOTHING;
      GET DIAGNOSTICS active_count = ROW_COUNT;
      notify := active_count = 1;
    END IF;
  END IF;
  RETURN public.lm_managed_allowance_result(p_uid, period, p_action_key, allowed)
    || jsonb_build_object('notify', notify);
END;
$function$;

CREATE OR REPLACE FUNCTION public.complete_lm_managed_action(p_uid text, p_action_key text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE period date := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;
BEGIN
  UPDATE public.lm_managed_action_ledger SET status = 'succeeded', completed_at = COALESCE(completed_at, clock_timestamp())
   WHERE uid = p_uid AND period_start = period AND action_key = p_action_key;
  RETURN public.lm_managed_allowance_result(p_uid, period, p_action_key, FOUND)
    || jsonb_build_object('notify', false);
END;
$function$;

CREATE OR REPLACE FUNCTION public.release_lm_managed_action(p_uid text, p_action_key text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE period date := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;
BEGIN
  DELETE FROM public.lm_managed_action_ledger
   WHERE uid = p_uid AND period_start = period AND action_key = p_action_key AND status = 'pending';
  RETURN public.lm_managed_allowance_result(p_uid, period, p_action_key, true)
    || jsonb_build_object('notify', false);
END;
$function$;

REVOKE ALL ON TABLE public.lm_managed_action_ledger FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.lm_managed_allowance_notice FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.lm_managed_allowance_result(text,date,text,boolean) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reserve_lm_managed_action(text,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_lm_managed_action(text,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_lm_managed_action(text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_lm_managed_action(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_lm_managed_action(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_lm_managed_action(text,text) TO service_role;
