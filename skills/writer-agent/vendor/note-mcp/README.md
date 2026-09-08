# Vendored note-mcp source

This directory contains the Python source required by the Writer loop's note.com adapter.
The reproducible upstream base is `https://github.com/drillan/note-mcp` commit
`528914f169223addc1ff14d0e32862d21514c195`. Life Manager then applies the two local auth
commits preserved verbatim in `anicca-local-auth-fixes.patch`:

- `62765f383aa0b91d042ea2c810204189a06cc276`
- `69260af68ee818b977b58287fe89f426fd80792b`

Those two commits were never published to the upstream remote; the patch is therefore their
portable source of truth. To reproduce, check out the upstream base and run
`git am anicca-local-auth-fixes.patch`. In the vendored tree, Life Manager additionally
backports the upstream Python 3.12 type-parameter syntax in `decorators.py` and
`api/articles.py` to equivalent `ParamSpec`/`TypeVar` declarations so the self-host runtime
remains Python 3.11 compatible.
The upstream package declares the MIT license.

Life Manager changes `note_mcp/__init__.py` to avoid eagerly importing the unused MCP server;
the Writer loop imports the API, authentication and model modules directly. Upstream Docker,
development scripts, tests, caches and MCP-server-only packaging are intentionally excluded.
