# Install once, then run every repository gate with the pinned Python environment.
python := "plugins/wise/engine/.venv/bin/python"

default: check

install:
    just --justfile plugins/wise/engine/justfile install

validate:
    {{python}} scripts/validate_repo.py

test:
    {{python}} -m pytest -c plugins/wise/engine/pyproject.toml plugins/wise/engine/tests plugins/wise/tests -q

engine-check:
    just --justfile plugins/wise/engine/justfile typecheck lint fmt-check

syntax:
    {{python}} -m py_compile plugins/wise/scripts/*.py scripts/*.py
    {{python}} -m json.tool .claude-plugin/marketplace.json > /dev/null
    {{python}} -m json.tool plugins/wise/.claude-plugin/plugin.json > /dev/null
    {{python}} -m json.tool plugins/wise/.mcp.json > /dev/null
    for f in plugins/wise/engine/engine.sh plugins/wise/scripts/*.sh plugins/wise/hooks/*.sh; do bash -n "$f"; done

check: validate test engine-check syntax
    @echo "all checks passed"
