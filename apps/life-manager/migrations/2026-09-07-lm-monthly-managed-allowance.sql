-- Monthly customer allowance. One tenant/event key consumes one unit only after user value succeeds.
-- Pending reservations close concurrency races and expire after 15 minutes if a worker disappears.

CREATE TABLE IF NOT EXISTS public.lm_managed_action_ledger (
  uid text NOT NULL,
  period_start date NOT NULL,
  action_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'succeeded')),
  reservation_token uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  PRIMARY KEY (uid, period_start, action_key)
);

ALTER TABLE public.lm_managed_action_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lm_managed_action_ledger
  ADD COLUMN IF NOT EXISTS reservation_token uuid;
UPDATE public.lm_managed_action_ledger SET reservation_token = gen_random_uuid()
 WHERE reservation_token IS NULL;
ALTER TABLE public.lm_managed_action_ledger
  ALTER COLUMN reservation_token SET DEFAULT gen_random_uuid(),
  ALTER COLUMN reservation_token SET NOT NULL;

CREATE TABLE IF NOT EXISTS public.lm_managed_allowance_notice (
  uid text NOT NULL,
  period_start date NOT NULL,
  notice_kind text NOT NULL CHECK (notice_kind IN ('eighty', 'exhausted')),
  action_key text NOT NULL,
  claim_token uuid,
  claimed_at timestamptz,
  telegram_message_id bigint CHECK (telegram_message_id IS NULL OR telegram_message_id > 0),
  delivery_state text NOT NULL DEFAULT 'pending'
    CHECK (delivery_state IN ('pending', 'claimed', 'delivery_unknown', 'delivered')),
  delivered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (uid, period_start, notice_kind)
);

ALTER TABLE public.lm_managed_allowance_notice ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lm_managed_allowance_notice
  ADD COLUMN IF NOT EXISTS delivery_state text NOT NULL DEFAULT 'pending';
ALTER TABLE public.lm_managed_allowance_notice
  DROP CONSTRAINT IF EXISTS lm_managed_allowance_notice_delivery_state_check;
ALTER TABLE public.lm_managed_allowance_notice
  ADD CONSTRAINT lm_managed_allowance_notice_delivery_state_check
  CHECK (delivery_state IN ('pending', 'claimed', 'delivery_unknown', 'delivered'));

-- This migration replaced the initial two-argument draft before production rollout. Drop those
-- overloads explicitly so a partially applied preview database cannot retain an ownerless path.
DROP FUNCTION IF EXISTS public.complete_lm_managed_action(text, text);
DROP FUNCTION IF EXISTS public.release_lm_managed_action(text, text);
DROP FUNCTION IF EXISTS public.lm_managed_allowance_result(text, date, text, boolean);
DROP FUNCTION IF EXISTS public.record_lm_managed_allowance_notice(text, text, uuid, bigint);
DROP FUNCTION IF EXISTS public.release_lm_managed_allowance_notice(text, text, uuid);

CREATE OR REPLACE FUNCTION public.lm_managed_period(p_uid text)
RETURNS date LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $function$
  SELECT date_trunc('month', clock_timestamp() AT TIME ZONE COALESCE(
    (SELECT z.name FROM public.lm_panel_preferences p
      JOIN pg_catalog.pg_timezone_names z ON z.name = p.call_time_zone
     WHERE p.uid = p_uid),
    'UTC'
  ))::date;
$function$;

CREATE OR REPLACE FUNCTION public.lm_managed_allowance_result(
  p_uid text,
  p_period_start date,
  p_action_key text,
  p_allowed boolean,
  p_reservation_token uuid DEFAULT NULL,
  p_state text DEFAULT NULL
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
    'actionKey', p_action_key,
    'reservationToken', p_reservation_token,
    'state', p_state,
    'alreadyCompleted', p_state = 'succeeded'
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
  period date;
  cap integer;
  active_count integer;
  existing_status text;
  reservation uuid;
  allowed boolean := false;
BEGIN
  IF p_uid IS NULL OR btrim(p_uid) = '' OR char_length(p_uid) > 256
     OR p_action_key IS NULL OR btrim(p_action_key) = '' OR char_length(p_action_key) > 512 THEN
    RAISE EXCEPTION 'invalid managed action identity';
  END IF;
  period := public.lm_managed_period(p_uid);
  PERFORM pg_advisory_xact_lock(hashtextextended(p_uid || ':' || period::text, 0));
  SELECT CASE WHEN paid THEN 500 ELSE 30 END INTO cap FROM public.lm_users WHERE uid = p_uid;
  IF cap IS NULL THEN RAISE EXCEPTION 'unknown tenant'; END IF;
  DELETE FROM public.lm_managed_action_ledger
    WHERE uid = p_uid AND period_start = period AND status = 'pending'
      AND created_at < clock_timestamp() - INTERVAL '15 minutes';
  SELECT status INTO existing_status FROM public.lm_managed_action_ledger
    WHERE uid = p_uid AND period_start = period AND action_key = p_action_key;
  IF existing_status IS NOT NULL THEN
    -- A retry may observe the result, but never receives another worker's owner token.
    -- Only the worker that inserted the pending row may execute or finalize the provider effect.
    RETURN public.lm_managed_allowance_result(p_uid, period, p_action_key,
      existing_status = 'succeeded', NULL, existing_status);
  ELSE
    SELECT count(*) INTO active_count FROM public.lm_managed_action_ledger
      WHERE uid = p_uid AND period_start = period;
    IF active_count < cap THEN
      reservation := gen_random_uuid();
      INSERT INTO public.lm_managed_action_ledger(uid, period_start, action_key, status, reservation_token)
      VALUES (p_uid, period, p_action_key, 'pending', reservation);
      allowed := true;
    ELSE
      DELETE FROM public.lm_managed_allowance_notice
       WHERE uid = p_uid AND period_start = period AND notice_kind = 'eighty'
         AND delivery_state = 'pending' AND delivered_at IS NULL;
      INSERT INTO public.lm_managed_allowance_notice(uid, period_start, notice_kind, action_key)
      VALUES (p_uid, period, 'exhausted', p_action_key) ON CONFLICT DO NOTHING;
    END IF;
  END IF;
  RETURN public.lm_managed_allowance_result(p_uid, period, p_action_key, allowed, reservation,
    CASE WHEN allowed THEN 'pending' ELSE 'exhausted' END);
END;
$function$;

CREATE OR REPLACE FUNCTION public.complete_lm_managed_action(
  p_uid text, p_action_key text, p_period_start date, p_reservation_token uuid
)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE
  cap integer;
  succeeded integer;
  completed boolean := false;
BEGIN
  UPDATE public.lm_managed_action_ledger SET status = 'succeeded', completed_at = COALESCE(completed_at, clock_timestamp())
   WHERE uid = p_uid AND period_start = p_period_start AND action_key = p_action_key
     AND reservation_token = p_reservation_token AND status = 'pending';
  IF FOUND THEN
    completed := true;
    SELECT CASE WHEN paid THEN 500 ELSE 30 END INTO cap FROM public.lm_users WHERE uid = p_uid;
    SELECT count(*) INTO succeeded FROM public.lm_managed_action_ledger
      WHERE uid = p_uid AND period_start = p_period_start AND status = 'succeeded';
    IF succeeded >= CEIL(cap * 0.8)::integer AND succeeded < cap THEN
      INSERT INTO public.lm_managed_allowance_notice(uid, period_start, notice_kind, action_key)
      VALUES (p_uid, p_period_start, 'eighty', p_action_key) ON CONFLICT DO NOTHING;
    END IF;
  ELSE
    -- Provider webhooks replay. The exact owner may acknowledge its already-completed row;
    -- a different token still fails closed.
    SELECT EXISTS(
      SELECT 1 FROM public.lm_managed_action_ledger
       WHERE uid = p_uid AND period_start = p_period_start AND action_key = p_action_key
         AND reservation_token = p_reservation_token AND status = 'succeeded'
    ) INTO completed;
  END IF;
  RETURN public.lm_managed_allowance_result(p_uid, p_period_start, p_action_key, completed, NULL,
    CASE WHEN completed THEN 'succeeded' ELSE 'owner_mismatch' END);
END;
$function$;

CREATE OR REPLACE FUNCTION public.release_lm_managed_action(
  p_uid text, p_action_key text, p_period_start date, p_reservation_token uuid
)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
BEGIN
  DELETE FROM public.lm_managed_action_ledger
   WHERE uid = p_uid AND period_start = p_period_start AND action_key = p_action_key AND status = 'pending'
     AND reservation_token = p_reservation_token;
  RETURN public.lm_managed_allowance_result(p_uid, p_period_start, p_action_key, FOUND, NULL,
    CASE WHEN FOUND THEN 'released' ELSE 'owner_mismatch' END);
END;
$function$;

CREATE OR REPLACE FUNCTION public.claim_lm_managed_allowance_notice(p_uid text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE
  period date;
  picked public.lm_managed_allowance_notice%ROWTYPE;
BEGIN
  period := public.lm_managed_period(p_uid);
  -- A crash before a worker can classify its send is retained as ambiguity, never blindly retried.
  UPDATE public.lm_managed_allowance_notice
     SET delivery_state = 'delivery_unknown'
   WHERE uid = p_uid AND period_start = period AND delivery_state = 'claimed'
     AND delivered_at IS NULL AND claimed_at < clock_timestamp() - INTERVAL '15 minutes';
  SELECT * INTO picked FROM public.lm_managed_allowance_notice
   WHERE uid = p_uid AND period_start = period AND delivered_at IS NULL AND claim_token IS NULL
     AND delivery_state = 'pending'
     AND (notice_kind = 'exhausted' OR NOT EXISTS (
       SELECT 1 FROM public.lm_managed_allowance_notice AS exhausted
        WHERE exhausted.uid = p_uid AND exhausted.period_start = period
          AND exhausted.notice_kind = 'exhausted'
     ))
   ORDER BY CASE notice_kind WHEN 'exhausted' THEN 0 ELSE 1 END
   LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF NOT FOUND THEN RETURN NULL; END IF;
  UPDATE public.lm_managed_allowance_notice
     SET claim_token = gen_random_uuid(), claimed_at = clock_timestamp(), delivery_state = 'claimed'
   WHERE uid = picked.uid AND period_start = picked.period_start AND notice_kind = picked.notice_kind
   RETURNING * INTO picked;
  RETURN jsonb_build_object('kind', picked.notice_kind, 'claimToken', picked.claim_token::text,
    'periodStart', picked.period_start::text, 'resetAt', (picked.period_start + INTERVAL '1 month')::date::text,
    'actionKey', picked.action_key,
    'used', (SELECT count(*) FROM public.lm_managed_action_ledger l WHERE l.uid = p_uid
      AND l.period_start = period AND l.status = 'succeeded'),
    'limit', (SELECT CASE WHEN paid THEN 500 ELSE 30 END FROM public.lm_users WHERE uid = p_uid),
    'paid', (SELECT COALESCE(paid, false) FROM public.lm_users WHERE uid = p_uid));
END;
$function$;

CREATE OR REPLACE FUNCTION public.record_lm_managed_allowance_notice(
  p_uid text, p_notice_kind text, p_period_start date, p_claim_token uuid, p_telegram_message_id bigint
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
BEGIN
  IF p_notice_kind NOT IN ('eighty', 'exhausted') OR p_telegram_message_id IS NULL OR p_telegram_message_id <= 0 THEN
    RETURN false;
  END IF;
  UPDATE public.lm_managed_allowance_notice
     SET telegram_message_id = p_telegram_message_id, delivered_at = COALESCE(delivered_at, clock_timestamp()),
       delivery_state = 'delivered'
   WHERE uid = p_uid AND period_start = p_period_start AND notice_kind = p_notice_kind
     AND claim_token = p_claim_token
     AND (telegram_message_id IS NULL OR telegram_message_id = p_telegram_message_id);
  RETURN FOUND;
END;
$function$;

CREATE OR REPLACE FUNCTION public.release_lm_managed_allowance_notice(
  p_uid text, p_notice_kind text, p_period_start date, p_claim_token uuid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
BEGIN
  UPDATE public.lm_managed_allowance_notice SET claim_token = NULL, claimed_at = NULL, delivery_state = 'pending'
   WHERE uid = p_uid AND period_start = p_period_start AND notice_kind = p_notice_kind
     AND claim_token = p_claim_token AND delivered_at IS NULL;
  RETURN FOUND;
END;
$function$;

CREATE OR REPLACE FUNCTION public.mark_lm_managed_allowance_notice_unknown(
  p_uid text, p_notice_kind text, p_period_start date, p_claim_token uuid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
BEGIN
  UPDATE public.lm_managed_allowance_notice SET delivery_state = 'delivery_unknown'
   WHERE uid = p_uid AND period_start = p_period_start AND notice_kind = p_notice_kind
     AND claim_token = p_claim_token AND delivery_state = 'claimed' AND delivered_at IS NULL;
  RETURN FOUND;
END;
$function$;

-- Paid phone fair-use allowance. A reservation holds the maximum seconds that one accepted
-- Telnyx leg may consume; provider time_limit_secs enforces that exact bound after answer.
CREATE TABLE IF NOT EXISTS public.lm_voice_allowance_ledger (
  uid text NOT NULL,
  period_start date NOT NULL,
  call_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'accepted', 'succeeded')),
  reservation_token uuid NOT NULL DEFAULT gen_random_uuid(),
  reserved_seconds integer NOT NULL CHECK (reserved_seconds BETWEEN 1 AND 120),
  connected_seconds integer CHECK (connected_seconds BETWEEN 0 AND reserved_seconds),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  PRIMARY KEY (uid, period_start, call_key)
);
ALTER TABLE public.lm_voice_allowance_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lm_voice_allowance_ledger
  DROP CONSTRAINT IF EXISTS lm_voice_allowance_ledger_status_check;
ALTER TABLE public.lm_voice_allowance_ledger
  ADD CONSTRAINT lm_voice_allowance_ledger_status_check
  CHECK (status IN ('pending', 'accepted', 'succeeded'));

CREATE OR REPLACE FUNCTION public.lm_voice_allowance_result(
  p_uid text, p_period_start date, p_call_key text, p_allowed boolean,
  p_allowed_seconds integer DEFAULT 0, p_reservation_token uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
  SELECT jsonb_build_object(
    'allowed', p_allowed,
    'usedSeconds', COALESCE(sum(connected_seconds) FILTER (WHERE status = 'succeeded'), 0)::integer,
    'limitSeconds', 3600,
    'allowedSeconds', COALESCE(p_allowed_seconds, 0),
    'periodStart', p_period_start::text,
    'resetAt', (p_period_start + INTERVAL '1 month')::date::text,
    'callKey', p_call_key,
    'reservationToken', p_reservation_token
  ) FROM public.lm_voice_allowance_ledger
  WHERE uid = p_uid AND period_start = p_period_start;
$function$;

CREATE OR REPLACE FUNCTION public.reserve_lm_voice_allowance(p_uid text, p_call_key text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE
  period date;
  consumed integer;
  remaining integer;
  seconds integer;
  token uuid;
  is_paid boolean;
BEGIN
  IF p_uid IS NULL OR btrim(p_uid) = '' OR char_length(p_uid) > 256
     OR p_call_key IS NULL OR btrim(p_call_key) = '' OR char_length(p_call_key) > 512 THEN
    RAISE EXCEPTION 'invalid voice allowance identity';
  END IF;
  period := public.lm_managed_period(p_uid);
  PERFORM pg_advisory_xact_lock(hashtextextended(p_uid || ':' || period::text || ':voice', 0));
  SELECT paid IS TRUE INTO is_paid FROM public.lm_users WHERE uid = p_uid;
  IF is_paid IS DISTINCT FROM true THEN
    RETURN public.lm_voice_allowance_result(p_uid, period, p_call_key, false, 0, NULL);
  END IF;
  DELETE FROM public.lm_voice_allowance_ledger
   WHERE uid = p_uid AND period_start = period AND status = 'pending'
     AND created_at < clock_timestamp() - INTERVAL '15 minutes';
  IF EXISTS (SELECT 1 FROM public.lm_voice_allowance_ledger
      WHERE uid = p_uid AND period_start = period AND call_key = p_call_key) THEN
    RETURN public.lm_voice_allowance_result(p_uid, period, p_call_key, false, 0, NULL);
  END IF;
  SELECT COALESCE(sum(CASE WHEN status IN ('pending', 'accepted') THEN reserved_seconds ELSE connected_seconds END), 0)::integer
    INTO consumed FROM public.lm_voice_allowance_ledger WHERE uid = p_uid AND period_start = period;
  remaining := 3600 - consumed;
  IF remaining <= 0 THEN
    RETURN public.lm_voice_allowance_result(p_uid, period, p_call_key, false, 0, NULL);
  END IF;
  seconds := LEAST(120, remaining);
  token := gen_random_uuid();
  INSERT INTO public.lm_voice_allowance_ledger
    (uid, period_start, call_key, status, reservation_token, reserved_seconds)
  VALUES (p_uid, period, p_call_key, 'pending', token, seconds);
  RETURN public.lm_voice_allowance_result(p_uid, period, p_call_key, true, seconds, token);
END;
$function$;

CREATE OR REPLACE FUNCTION public.accept_lm_voice_allowance(
  p_uid text, p_call_key text, p_period_start date, p_reservation_token uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE seconds integer;
BEGIN
  UPDATE public.lm_voice_allowance_ledger SET status = 'accepted'
   WHERE uid = p_uid AND period_start = p_period_start AND call_key = p_call_key
     AND reservation_token = p_reservation_token AND status = 'pending'
   RETURNING reserved_seconds INTO seconds;
  IF seconds IS NULL THEN
    SELECT reserved_seconds INTO seconds FROM public.lm_voice_allowance_ledger
     WHERE uid = p_uid AND period_start = p_period_start AND call_key = p_call_key
       AND reservation_token = p_reservation_token AND status = 'accepted';
  END IF;
  RETURN public.lm_voice_allowance_result(p_uid, p_period_start, p_call_key, seconds IS NOT NULL,
    COALESCE(seconds, 0), NULL);
END;
$function$;

CREATE OR REPLACE FUNCTION public.complete_lm_voice_allowance(
  p_uid text, p_call_key text, p_period_start date, p_reservation_token uuid, p_connected_seconds integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE settled integer;
BEGIN
  UPDATE public.lm_voice_allowance_ledger
     SET status = 'succeeded', connected_seconds = LEAST(reserved_seconds, GREATEST(0, p_connected_seconds)),
         completed_at = COALESCE(completed_at, clock_timestamp())
   WHERE uid = p_uid AND period_start = p_period_start AND call_key = p_call_key
     AND reservation_token = p_reservation_token AND status = 'accepted'
   RETURNING connected_seconds INTO settled;
  IF settled IS NULL THEN
    SELECT connected_seconds INTO settled FROM public.lm_voice_allowance_ledger
     WHERE uid = p_uid AND period_start = p_period_start AND call_key = p_call_key
       AND reservation_token = p_reservation_token AND status = 'succeeded';
  END IF;
  RETURN public.lm_voice_allowance_result(p_uid, p_period_start, p_call_key, settled IS NOT NULL,
    COALESCE(settled, 0), NULL);
END;
$function$;

CREATE OR REPLACE FUNCTION public.release_lm_voice_allowance(
  p_uid text, p_call_key text, p_period_start date, p_reservation_token uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $function$
DECLARE released boolean;
BEGIN
  DELETE FROM public.lm_voice_allowance_ledger
   WHERE uid = p_uid AND period_start = p_period_start AND call_key = p_call_key
     AND reservation_token = p_reservation_token AND status IN ('pending', 'accepted');
  released := FOUND;
  RETURN public.lm_voice_allowance_result(p_uid, p_period_start, p_call_key, released, 0, NULL);
END;
$function$;

REVOKE ALL ON TABLE public.lm_managed_action_ledger FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.lm_managed_allowance_notice FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.lm_voice_allowance_ledger FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.lm_managed_allowance_result(text,date,text,boolean,uuid,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.lm_managed_period(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reserve_lm_managed_action(text,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_lm_managed_action(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_lm_managed_action(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_lm_managed_allowance_notice(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.record_lm_managed_allowance_notice(text,text,date,uuid,bigint) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_lm_managed_allowance_notice(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.mark_lm_managed_allowance_notice_unknown(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.lm_voice_allowance_result(text,date,text,boolean,integer,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reserve_lm_voice_allowance(text,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.accept_lm_voice_allowance(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_lm_voice_allowance(text,text,date,uuid,integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_lm_voice_allowance(text,text,date,uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_lm_managed_action(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_lm_managed_action(text,text,date,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_lm_managed_action(text,text,date,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_lm_managed_allowance_notice(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.record_lm_managed_allowance_notice(text,text,date,uuid,bigint) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_lm_managed_allowance_notice(text,text,date,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.mark_lm_managed_allowance_notice_unknown(text,text,date,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.reserve_lm_voice_allowance(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.accept_lm_voice_allowance(text,text,date,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_lm_voice_allowance(text,text,date,uuid,integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_lm_voice_allowance(text,text,date,uuid) TO service_role;
