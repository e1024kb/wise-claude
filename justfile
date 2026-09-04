# wise-claude — task runner (https://just.systems)
# Alternative to invoking the scripts directly. `just <recipe>`.

# Default: validate + test.
default: validate test

# Structural validation of the repo + plugin.
validate:
    python3 scripts/validate_repo.py

# Run the engine test suite.
test:
    python3 -m pytest plugins/wise/tests -q

# TypeScript engine: typecheck + lint + format check + tests (bun if present, else npm / node 24).
engine-check:
    cd plugins/wise/engine && if command -v bun >/dev/null 2>&1; then bun install --frozen-lockfile && bun run check; else npm install --no-audit --no-fund && npm run check; fi

# Everything CI runs, locally.
check: validate test engine-check
    @echo "all checks passed"
