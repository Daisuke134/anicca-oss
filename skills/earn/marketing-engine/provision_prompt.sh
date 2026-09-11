#!/usr/bin/env bash

require_ig_isolated_context() {
  local port="${1:?isolated CDP port is required}"
  local context_id="${2:?isolated context id is required}"
  if [ "$port" = "9222" ]; then
    printf '%s\n' 'ERROR: refusing Instagram signup/login on :9222 main context; dedicated isolated port required' >&2
    return 64
  fi
  case "$context_id" in
    ''|main|default)
      printf '%s\n' 'ERROR: refusing Instagram signup/login without a dedicated isolated context id' >&2
      return 64
      ;;
  esac
}

render_ig_provision_prompt() {
  local state_file="${IG_PROVISION_ACCOUNT_STATE_FILE:?IG_PROVISION_ACCOUNT_STATE_FILE is required}"
  local handle_prefix="${IG_PROVISION_HANDLE_PREFIX:?IG_PROVISION_HANDLE_PREFIX is required}"
  local instance="${IG_PROVISION_INSTANCE:?IG_PROVISION_INSTANCE is required}"
  local gmail_plus_tag_prefix="${IG_PROVISION_GMAIL_PLUS_TAG_PREFIX:?IG_PROVISION_GMAIL_PLUS_TAG_PREFIX is required}"
  local bio_text="${IG_PROVISION_BIO_TEXT:?IG_PROVISION_BIO_TEXT is required}"
  local browser_instructions="${IG_PROVISION_BROWSER_INSTRUCTIONS:?IG_PROVISION_BROWSER_INSTRUCTIONS is required}"
  local profile_prefix="${IG_PROVISION_PROFILE_PREFIX:-${instance}-mkt}"
  local cooked_marker="${IG_PROVISION_COOKED_MARKER:-}"
  local provision_reason="${IG_PROVISION_REASON:-none}"
  local reach_marker="${IG_PROVISION_REACH_MARKER:-}"
  local last_pass_marker="${IG_PROVISION_LAST_PASS_MARKER:-}"
  local telegram_target="${IG_PROVISION_TELEGRAM_TARGET:-}"
  local isolated_port="${IG_PROVISION_PORT:?IG_PROVISION_PORT is required}"
  local context_id="${IG_PROVISION_CONTEXT_ID:?IG_PROVISION_CONTEXT_ID is required}"
  local browser_identity="${IG_PROVISION_BROWSER_IDENTITY:-$context_id}"
  local owner_gmail="${IG_PROVISION_GMAIL_ACCOUNT:-${LIFE_MANAGER_GMAIL_ACCOUNT:?LIFE_MANAGER_GMAIL_ACCOUNT is required}}"
  local gmail_local="${owner_gmail%@*}"
  require_ig_isolated_context "$isolated_port" "$context_id" || return

  cat <<EOF
STEP PROVISION (1-loop-1-account, replace-on-cold, ZERO human): ${state_file} has no usable ready/warming account. Follow this repository-owned email-only flow using ${LIFE_MANAGER_REPO}/skills/browser/scripts/cdp.py and ${LIFE_MANAGER_REPO}/skills/earn/capafy-marketing/scripts/setup_profile.py. Create one brand-new ${instance} IG account on residential home IP with NO proxy. Use a unique handle beginning with ${handle_prefix}. Before choosing email, inspect ${state_file} plus existing account/credential failure reasons. Historical rejection is proof: if prior rows say Instagram rejected the base Gmail plus-address and dot-address, skip directly to the fresh Gmail account fallback below; do not spend another pass reconfirming either alias type. Otherwise first try one fresh Gmail plus-address ${gmail_local}+${gmail_plus_tag_prefix}<random-tag>@gmail.com. If Instagram visibly labels that exact, length-verified address invalid, do not retry another plus-address. Use the Gmail dot-address fallback: generate a previously unused address by this rule: insert dots only between letters of ${gmail_local} (never remove, reorder, or change a letter), and keep @gmail.com. Gmail delivers it to the same ${owner_gmail} inbox while Instagram receives a distinct address. Check existing account rows and credential files to avoid reusing a dot pattern. If one exact, length-verified dot-address is also visibly invalid, use the fresh Gmail account fallback: in the same dedicated isolated browser, create one genuinely new @gmail.com Google account, save its credentials atomically to ~/.cloak/email-capafy-<gmail-user>.json with mode 600, then use that address for Instagram and read Instagram OTP from Gmail Web in the same isolated browser. Do not add the new email account to gog during this pass. If Google completes without phone/CAPTCHA, continue automatically; never invent success if Google forces phone or CAPTCHA—report provision-blocked:<exact Google gate> and retain replacement_requested=true. For the original inbox path, read the 6-digit OTP with: gog gmail search --account ${owner_gmail} "instagram in:anywhere newer_than:1h" --max 3 --plain. The in:anywhere term is mandatory because OTP can land in spam. Never print or echo the password or OTP; pass secrets only as quoted command arguments and verify only their lengths. Do not use phone flow unless Instagram forces it; if phone or text-CAPTCHA is forced, stop and report provision-blocked:<reason>. BROWSER ISOLATION: ${browser_instructions} Fill every text field with the repository cdp.py command replace <tid> <selector> <value> [visible-index]. Never retry a field with insert: insert appends and can silently duplicate the email. After each replace, read the field value length back and require it to equal the intended value length before continuing. For any visible text button such as 送信, use cdp.py clicktexttrusted <tid> <exact-text> -1, which resolves the current rect and trusted-clicks atomically. Never reuse a recorded coordinate after any scroll, screenshot, focus, or layout change. Set DOB with trusted clicks, create the unique handle, and run the repository setup_profile.py with a \$0 PIL monogram avatar plus this one-line bio containing NO link: "${bio_text}". Save signup credentials to ~/.cloak/ig-<handle>.json as JSON with username, name, email, pw, dob, and created.

CUSTOM-DOMAIN PRECEDENCE: contact@aniccaai.com is a durable custom-domain recovery address that forwards into ${owner_gmail} and is currently absent from Instagram credential files. If alias rejection is already recorded, use this custom-domain recovery address before attempting to create Gmail. Read its Instagram OTP with: gog gmail search --account ${owner_gmail} "to:contact@aniccaai.com instagram in:anywhere newer_than:1h" --max 5 --plain. If history records a Google QR-device gate, never retry Gmail creation in a later pass; use contact@aniccaai.com when unused, otherwise report the exact structural gate without looping.

CREDENTIAL DURABILITY: Stage credentials before the first Instagram field mutation by atomically writing the final ~/.cloak/ig-<handle>.json with mode 600 and fields username, name, email, pw, dob, and created. A browser signup is not allowed to outrun its recovery credential. If signup later fails, retain that credential for audit while the account row remains provision_failed/session_failed. Do not use status as a zsh variable; status is read-only in zsh. Use row_status or write the registry with Python directly.

DAY 1 SIGNUP ONLY: account creation day is day1. Do not run Client().login. Do not run login_by_sessionid. Do not call get_timeline_feed() or dump_settings(). Do not create ~/.cloak/instagrapi-<handle>.json. The isolated browser session is the only session owner on day1. warmer.py creates the one-and-only golden instagrapi session after the account reaches day3.

STATE WRITE: use ${state_file} only and preserve every existing row. Use the preflight-validated dedicated port ${isolated_port}, context id ${context_id}, browser identity ${browser_identity}, and a unique profile beginning with ${profile_prefix}. After signup and profile setup prove the account is LIVE in the browser, atomically append {"handle":"<handle>","profile":"<profile>","port":${isolated_port},"context_id":"${context_id}","browser_identity":"${browser_identity}","lang":"en","status":"warming","session_owner":"browser","instance":"${instance}","created":"<today YYYY-MM-DD>","started_warming":"<today YYYY-MM-DD>"}. Parse the full JSON after writing, confirm row count increased by one and the final row matches the new handle. Signup + profile + credential file + warming state row is the complete day1 success condition.
EOF

  if [ -n "$cooked_marker" ]; then
    cat <<EOF
If provision reason is ${provision_reason} and equals cooked-marker, first mark prior ready/warming rows poisoned_manual_backup with poisoned_reason and poisoned_at so only the new appended row is active. On success remove ${cooked_marker}${reach_marker:+ and remove ${reach_marker}} so replacement account restarts clean validation.
EOF
  fi

  cat <<EOF
FAILURE: if signup, profile setup, credential save, or state verification fails, append a best-effort row with status=provision_failed and failure reason. Never label it ready/warming.
EOF

  if [ -n "$cooked_marker" ]; then
    printf 'Ensure %s remains present on failure.\n' "$cooked_marker"
  fi
  if [ -n "$telegram_target" ]; then
    cat <<EOF
Send Telegram chat ${telegram_target} a success message containing handle, port, session_owner=browser, status_written=warming, started_warming=<today>, and that this pass posted nothing. On failure send exactly one provision-blocked:<reason> report.
EOF
  else
    printf '%s\n' 'Report handle + port + session_owner=browser + status_written=warming + started_warming, or provision-blocked:<reason>.'
  fi
  if [ -n "$last_pass_marker" ]; then
    printf 'Touch %s and stop this pass after the provision success/failure report.\n' "$last_pass_marker"
  fi
}
