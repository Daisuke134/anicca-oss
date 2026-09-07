-- Resolve a one-time Telegram Calendar OAuth state without requiring a browser session cookie.
-- Raw state remains browser-only; this service-role RPC receives only its SHA-256 digest.
CREATE OR REPLACE FUNCTION public.claim_lm_telegram_oauth_state(p_state_hash text)
RETURNS TABLE(uid text, chat_id text)
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
     AND EXISTS (
       SELECT 1 FROM public.lm_users AS users
        WHERE users.uid = state.uid
          AND users.telegram_chat_id::text = state.chat_id
     )
  RETURNING state.uid, state.chat_id;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_lm_telegram_oauth_state(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_lm_telegram_oauth_state(text) TO service_role;
