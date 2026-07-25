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

# Everything CI runs, locally.
check: validate test
    @echo "all checks passed"
