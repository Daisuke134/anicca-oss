#!/usr/bin/env python3
"""Build the static Capafy online-skills landing page."""

import html
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[4]))
CAPAFY_HTTP = str(REPO_ROOT / "skills/capafy-autopublish/vendor/capafy-user/scripts/capafy_http.py")
OUTPUT_FILE = Path(__file__).resolve().parents[1] / "site" / "index.html"
GO_URL_FMT = "/go/{agent_id}"

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f4f2ed;
  --surface: rgba(255, 255, 255, 0.78);
  --surface-strong: #ffffff;
  --text: #171915;
  --muted: #666b61;
  --line: rgba(23, 25, 21, 0.12);
  --accent: #2457e6;
  --accent-text: #ffffff;
  --shadow: 0 18px 48px rgba(32, 35, 29, 0.08);
}

* {
  box-sizing: border-box;
}

html {
  background: var(--bg);
  scroll-behavior: smooth;
}

body {
  min-width: 280px;
  min-height: 100vh;
  margin: 0;
  color: var(--text);
  background:
    radial-gradient(circle at 15% 0%, rgba(36, 87, 230, 0.11), transparent 34rem),
    var(--bg);
  font-family: "Avenir Next", Avenir, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.page {
  width: min(100%, 48rem);
  margin: 0 auto;
  padding: clamp(2.5rem, 8vw, 5.5rem) 1rem 2rem;
}

.site-header {
  margin-bottom: clamp(2rem, 7vw, 4rem);
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1.75rem;
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.brand-mark {
  display: grid;
  width: 2rem;
  height: 2rem;
  place-items: center;
  border-radius: 0.65rem;
  color: var(--accent-text);
  background: var(--accent);
  box-shadow: 0 8px 22px rgba(36, 87, 230, 0.25);
  letter-spacing: 0;
}

h1,
h2,
p {
  margin-top: 0;
}

h1 {
  max-width: 12ch;
  margin-bottom: 1rem;
  font-size: clamp(2.65rem, 12vw, 5.4rem);
  font-weight: 720;
  letter-spacing: -0.055em;
  line-height: 0.95;
  text-wrap: balance;
}

.tagline {
  max-width: 34rem;
  margin-bottom: 0;
  color: var(--muted);
  font-size: clamp(1.05rem, 4vw, 1.3rem);
  line-height: 1.55;
}

.skills {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0.85rem;
}

.skill-card {
  display: grid;
  gap: 1.15rem;
  padding: clamp(1.2rem, 5vw, 1.65rem);
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 1.25rem;
  background: var(--surface);
  box-shadow: var(--shadow);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  transition: border-color 180ms ease, transform 180ms ease, box-shadow 180ms ease;
}

.skill-card:hover {
  border-color: rgba(36, 87, 230, 0.38);
  transform: translateY(-2px);
  box-shadow: 0 22px 58px rgba(32, 35, 29, 0.12);
}

.skill-copy {
  min-width: 0;
}

.skill-card h2 {
  margin-bottom: 0.55rem;
  font-size: clamp(1.12rem, 4vw, 1.35rem);
  font-weight: 680;
  letter-spacing: -0.025em;
  line-height: 1.25;
}

.skill-description {
  display: -webkit-box;
  margin-bottom: 0;
  overflow: hidden;
  color: var(--muted);
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.skill-link {
  display: inline-flex;
  min-height: 2.85rem;
  align-items: center;
  justify-content: center;
  padding: 0.75rem 1rem;
  border-radius: 0.85rem;
  color: var(--accent-text);
  background: var(--accent);
  font-weight: 700;
  text-decoration: none;
  transition: filter 160ms ease, transform 160ms ease;
}

.skill-link:hover {
  filter: brightness(1.08);
}

.skill-link:active {
  transform: scale(0.985);
}

.skill-link:focus-visible {
  outline: 3px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 3px;
}

.site-footer {
  padding: 2.25rem 0 0.5rem;
  color: var(--muted);
  font-size: 0.9rem;
  text-align: center;
}

@media (min-width: 38rem) {
  .page {
    padding-inline: 1.5rem;
  }

  .skill-card {
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
  }

  .skill-link {
    min-width: 9.8rem;
  }
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #10120f;
    --surface: rgba(27, 30, 25, 0.82);
    --surface-strong: #1b1e19;
    --text: #f3f5ee;
    --muted: #a9afa2;
    --line: rgba(243, 245, 238, 0.12);
    --accent: #7ea1ff;
    --accent-text: #0d1839;
    --shadow: 0 20px 54px rgba(0, 0, 0, 0.26);
  }

  body {
    background:
      radial-gradient(circle at 15% 0%, rgba(126, 161, 255, 0.14), transparent 34rem),
      var(--bg);
  }

  .brand-mark {
    box-shadow: 0 8px 24px rgba(126, 161, 255, 0.2);
  }

  .skill-card:hover {
    border-color: rgba(126, 161, 255, 0.42);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.36);
  }
}

@media (prefers-reduced-motion: reduce) {
  html {
    scroll-behavior: auto;
  }

  *,
  *::before,
  *::after {
    transition-duration: 0.01ms !important;
  }
}
""".strip()


def _find_agent_list(value):
    if isinstance(value, list) and (not value or isinstance(value[0], dict)):
        return value
    if isinstance(value, dict):
        for nested in value.values():
            found = _find_agent_list(nested)
            if found is not None:
                return found
    return None


def _fetch_agents() -> list:
    result = subprocess.run(
        [sys.executable, CAPAFY_HTTP, "GET", "/agent/agents"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"capafy_http failed with exit {result.returncode}")

    start = min(
        (index for index in (result.stdout.find("{"), result.stdout.find("[")) if index >= 0),
        default=-1,
    )
    if start < 0:
        raise RuntimeError("no JSON in capafy_http output")

    payload = json.loads(result.stdout[start:])
    return _find_agent_list(payload) or []


def filter_online_agents(agents: list) -> list:
    online = []
    for agent in agents:
        if agent.get("agentStatus") != "online" or agent.get("agentId") is None:
            continue
        normalized = dict(agent)
        normalized["agentId"] = str(agent["agentId"])
        online.append(normalized)
    return sorted(
        online,
        key=lambda agent: ((agent.get("name") or "").casefold(), agent["agentId"]),
    )


def _render_card(agent: dict) -> str:
    agent_id = quote(agent["agentId"], safe="")
    name = html.escape((agent.get("name") or "Untitled skill").strip(), quote=True)
    description = " ".join((agent.get("desc") or "No description provided.").split())
    description = html.escape(description, quote=True)
    url = html.escape(GO_URL_FMT.format(agent_id=agent_id), quote=True)
    return f"""      <article class="skill-card">
        <div class="skill-copy">
          <h2>{name}</h2>
          <p class="skill-description">{description}</p>
        </div>
        <a class="skill-link" href="{url}" aria-label="Use {name}">Use this skill →</a>
      </article>"""


def render_html(agents: list) -> str:
    cards = "\n".join(_render_card(agent) for agent in agents)
    count = len(agents)
    skill_label = "skill" if count == 1 else "skills"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-capafy-skills-daily'; base-uri 'none'; form-action 'none'">
  <meta name="description" content="Browse Claude skills you can use today.">
  <title>Claude Skills Daily</title>
  <style nonce="capafy-skills-daily">
{CSS}
  </style>
</head>
<body>
  <div class="page">
    <header class="site-header">
      <div class="brand"><span class="brand-mark" aria-hidden="true">C</span> @useclaudeskills</div>
      <h1>Claude Skills Daily</h1>
      <p class="tagline">Sharing Claude skills you can use, every day.</p>
    </header>
    <main class="skills" aria-label="Available Claude skills">
{cards}
    </main>
    <footer class="site-footer">{count} {skill_label} available</footer>
  </div>
</body>
</html>
"""


def build(output_file: Path = OUTPUT_FILE) -> int:
    online = filter_online_agents(_fetch_agents())
    if not online:
        raise RuntimeError("no online listings")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(render_html(online), encoding="utf-8")
    allowed_agents = output_file.parent / "netlify" / "functions" / "allowed-agents.json"
    allowed_agents.parent.mkdir(parents=True, exist_ok=True)
    allowed_agents.write_text(
        json.dumps([agent["agentId"] for agent in online], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(online)


def main() -> int:
    try:
        count = build()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"ok": True, "online_skills": count, "output": str(OUTPUT_FILE)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
