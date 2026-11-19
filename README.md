# Mendr / Data Fix: autonomous data repair

Zero dependencies (Python 3.9+ standard library).

    python server.py            # http://localhost:8000
    python -m unittest discover -s tests
    python tests/e2e.py         # browser test (playwright + Chrome)

Deploy: Dockerfile (any container host), or Procfile (Render, Railway, Heroku, Fly).
Env: PORT, MAX_BODY_MB (default 25), AGENT_URL (optional live-search agent endpoint), QUIET=1.

What the engine does: trims whitespace, removes exact duplicates, fills numeric gaps with the median,
cleans currency/% symbols, replaces impossible ages, clamps only extreme (3x IQR) outliers, standardises
inconsistent spelling/case, ISO-formats dates, lowercases emails, flags invalid values instead of guessing,
and never rewrites ID-like columns or invents values for free-text columns. Every change is logged with
row, before, after and reason. If the API is unreachable the page falls back to an in-browser engine.
