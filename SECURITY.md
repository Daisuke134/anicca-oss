# Security policy

## Supported versions

The `main` branch is the only supported version. If you find an issue
in an older tag please reproduce against `main` before reporting.

## Reporting a vulnerability

Please email **contact@aniccaai.com** with:

- a description of the vulnerability
- the commit hash or version where it was found
- a proof-of-concept if you have one
- whether you'd like credit in the disclosure

We will acknowledge within 72 hours and aim to ship a fix within 14
days for verifiable issues.

## Out of scope

- Twilio / Pipecat / Gemini / Stripe / Wise / Telegram bot API
  configuration issues that belong on the upstream — please report
  to those vendors first.
- Misconfigurations of the user's own `~/.local/state/life-manager/.env`. This file
  is the user's responsibility; we provide `.env.example` as the
  template and document the keys in `README.md` and
  `docs/INSTALL_BOOTSTRAP.md`.

## In scope

- Anything in this repo that could leak a user's keys, location, or
  call history out of the private Life Manager state directory.
- Anything in the install scripts (`install.sh`, `uninstall.sh`) that
  could be hijacked into executing
  arbitrary commands.
- Any Python or Bash code path that could be exploited by a
  malicious gcal event content (= prompt injection into the call
  ctx).

## CI scans

Every PR runs:
- `gitleaks detect --config .gitleaks.toml`
- `trufflehog filesystem --no-update`
- A custom PII grep for the maintainer's personal identifiers

If you spot one of those checks misfiring as a false positive, please
add a precise allowlist rule + cite the public source that proves the
pattern is non-sensitive.

## Acknowledgements

Researchers credited in the fix commit message and (if they want) in
the README's acknowledgements section.
