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

-- One Telegram reply completes both required core writes or neither; no half-finished home stage.
CREATE OR REPLACE FUNCTION public.complete_lm_telegram_home(
  p_uid text, p_chat_id text, p_home_address text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE bound_uid text;
BEGIN
  IF p_home_address IS NULL OR trim(p_home_address) = '' OR char_length(trim(p_home_address)) > 240 THEN
    RETURN false;
  END IF;
  SELECT uid INTO bound_uid FROM public.lm_users
   WHERE uid = p_uid AND telegram_chat_id::text = p_chat_id
     AND calendar_provider = 'composio_gcal'
     AND tg_onboard_stage IN ('calendar', 'home')
     AND nullif(trim(coalesce(home_address, '')), '') IS NULL
   FOR UPDATE;
  IF bound_uid IS NULL THEN RETURN false; END IF;
  UPDATE public.lm_users SET home_address = trim(p_home_address), tg_onboard_stage = 'phone', updated_at = now()
   WHERE uid = p_uid;
  INSERT INTO public.lm_panel_preferences(uid, notifications_enabled, call_enabled)
  VALUES (p_uid, true, false)
  ON CONFLICT (uid) DO UPDATE SET notifications_enabled = true, call_enabled = false, updated_at = now();
  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.complete_lm_telegram_home(text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.complete_lm_telegram_home(text,text,text) TO service_role;
