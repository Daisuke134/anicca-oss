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

-- A new explicit /start replaces this tenant's unconsumed link, so a lost Telegram reply can recover.
CREATE OR REPLACE FUNCTION public.create_lm_telegram_oauth_state(
  p_state_hash text, p_uid text, p_chat_id text, p_expires_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE binding_uid text;
BEGIN
  IF p_state_hash IS NULL OR p_state_hash !~ '^[a-f0-9]{64}$'
     OR p_uid IS NULL OR p_uid = '' OR p_chat_id IS NULL OR p_chat_id = ''
     OR p_expires_at IS NULL OR p_expires_at <= now() THEN RETURN false; END IF;
  SELECT uid INTO binding_uid FROM public.lm_users
   WHERE uid = p_uid AND telegram_chat_id::text = p_chat_id FOR UPDATE;
  IF binding_uid IS NULL THEN RETURN false; END IF;
  DELETE FROM public.lm_panel_oauth_states
   WHERE uid = p_uid AND chat_id = p_chat_id AND provider = 'calendar' AND used_at IS NULL;
  INSERT INTO public.lm_panel_oauth_states(state_hash, uid, chat_id, provider, expires_at)
  VALUES (p_state_hash, p_uid, p_chat_id, 'calendar', p_expires_at);
  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.create_lm_telegram_oauth_state(text,text,text,timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_lm_telegram_oauth_state(text,text,text,timestamptz) TO service_role;
