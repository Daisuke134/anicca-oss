-- Bind Telegram Calendar consent and runtime execution to the exact Composio account created by that flow.
ALTER TABLE public.lm_users
  ADD COLUMN IF NOT EXISTS calendar_connected_account_id text;

ALTER TABLE public.lm_panel_oauth_states
  ADD COLUMN IF NOT EXISTS connected_account_id text;

CREATE OR REPLACE FUNCTION public.attach_lm_panel_oauth_account(
  p_state_hash text,
  p_uid text,
  p_chat_id text,
  p_connected_account_id text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
  IF p_state_hash IS NULL OR p_state_hash !~ '^[a-f0-9]{64}$'
     OR p_connected_account_id IS NULL OR p_connected_account_id !~ '^[A-Za-z0-9_-]{3,128}$' THEN
    RETURN false;
  END IF;
  UPDATE public.lm_panel_oauth_states AS state
     SET connected_account_id = p_connected_account_id
   WHERE state.state_hash = p_state_hash
     AND state.uid = p_uid
     AND state.chat_id = p_chat_id
     AND state.provider = 'calendar'
     AND state.used_at IS NULL
     AND state.expires_at > now()
     AND state.connected_account_id IS NULL
     AND EXISTS (
       SELECT 1 FROM public.lm_users AS users
        WHERE users.uid = p_uid AND users.telegram_chat_id::text = p_chat_id
     );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION public.attach_lm_panel_oauth_account(text,text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.attach_lm_panel_oauth_account(text,text,text,text) TO service_role;

DROP FUNCTION IF EXISTS public.claim_lm_telegram_oauth_state(text);
CREATE FUNCTION public.claim_lm_telegram_oauth_state(p_state_hash text)
RETURNS TABLE(uid text, chat_id text, connected_account_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_state_hash IS NULL OR p_state_hash !~ '^[a-f0-9]{64}$' THEN RETURN; END IF;

  RETURN QUERY
  UPDATE public.lm_panel_oauth_states AS state
     SET used_at = now()
   WHERE state.state_hash = p_state_hash
     AND state.provider = 'calendar'
     AND state.used_at IS NULL
     AND state.expires_at > now()
     AND state.connected_account_id IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM public.lm_users AS users
        WHERE users.uid = state.uid
          AND users.telegram_chat_id::text = state.chat_id
     )
  RETURNING state.uid, state.chat_id, state.connected_account_id;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_lm_telegram_oauth_state(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_lm_telegram_oauth_state(text) TO service_role;

CREATE OR REPLACE FUNCTION public.claim_lm_panel_oauth_account(
  p_state_hash text,
  p_uid text,
  p_chat_id text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE claimed_account_id text;
BEGIN
  IF p_state_hash IS NULL OR p_state_hash !~ '^[a-f0-9]{64}$' THEN RETURN NULL; END IF;
  UPDATE public.lm_panel_oauth_states AS state
     SET used_at = now()
   WHERE state.state_hash = p_state_hash
     AND state.uid = p_uid
     AND state.chat_id = p_chat_id
     AND state.provider = 'calendar'
     AND state.used_at IS NULL
     AND state.expires_at > now()
     AND state.connected_account_id IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM public.lm_users AS users
        WHERE users.uid = p_uid AND users.telegram_chat_id::text = p_chat_id
     )
  RETURNING state.connected_account_id INTO claimed_account_id;
  RETURN claimed_account_id;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_lm_panel_oauth_account(text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_lm_panel_oauth_account(text,text,text) TO service_role;

CREATE OR REPLACE FUNCTION public.sync_lm_panel_calendar_connection(
  p_uid text,
  p_chat_id text,
  p_status text,
  p_connected_account_id text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
  IF p_status <> 'ACTIVE' OR p_connected_account_id IS NULL
     OR p_connected_account_id !~ '^[A-Za-z0-9_-]{3,128}$' THEN
    RETURN false;
  END IF;
  UPDATE public.lm_users
     SET calendar_provider = 'composio_gcal',
         calendar_connected_account_id = p_connected_account_id,
         updated_at = now()
   WHERE uid = p_uid AND telegram_chat_id::text = p_chat_id;
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION public.sync_lm_panel_calendar_connection(text,text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sync_lm_panel_calendar_connection(text,text,text,text) TO service_role;
