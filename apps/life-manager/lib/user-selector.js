// lib/user-selector.js — C4 (VCSDD life-manager-cost-connect-reliability). SSOT for which calendar
// providers get wake calls. Pipedream Connect provisions users as `pipedream_gcal`; the old code
// hardcoded `eq.composio_gcal` at two sites, so Pipedream users got zero wakes. Both scheduler sites
// now share this one filter.
"use strict";

const WAKE_CALENDAR_PROVIDERS = ["composio_gcal", "pipedream_gcal"];
const CALLABLE_PHONE_RE = /^\+[1-9]\d{7,14}$/;

// Stored phone values must already be normalized E.164. Formatting/normalization belongs to
// onboarding; the scheduler only answers whether a value is safe to hand to the dial provider.
function isCallablePhone(value) {
  return typeof value === "string" && CALLABLE_PHONE_RE.test(value);
}

// PostgREST filter fragment selecting any supported calendar provider.
function calendarProviderFilter() {
  return `calendar_provider=in.(${WAKE_CALENDAR_PROVIDERS.join(",")})`;
}

// Scheduler eligibility is deliberately broader than paid allowance eligibility. Calendar reads,
// cached facts, settings and Telegram control remain available after a monthly allowance is used;
// the paid-provider boundary enforces the allowance separately.
function schedulerCohortFilter() {
  return calendarProviderFilter();
}

module.exports = { WAKE_CALENDAR_PROVIDERS, CALLABLE_PHONE_RE, isCallablePhone, calendarProviderFilter, schedulerCohortFilter };
