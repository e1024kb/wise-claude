# wise-claude — task runner (https://just.systems)
# Alternative to invoking the scripts directly. `just <recipe>`.

# Default: validate + test.
default: validate test

# Structural validation of the repo + every harness port.
validate:
    python3 scripts/validate_repo.py

# Run the Claude port's engine test suite.
test:
    python3 -m pytest harnesses/claude/wise/tests -q

# Advisory core ↔ port divergence report (always exits 0).
drift:
    python3 scripts/report_core_drift.py

# Install a harness port. Examples:
#   just install claude
#   just install codex
#   just install cursor
#   just install hermes
#   just install cursor project=./my-repo
install harness scope="user" project=".":
    ./install.sh {{harness}} --{{scope}} --project {{project}}

# Uninstall a harness port (removes exactly what install added).
uninstall harness scope="user" project=".":
    ./install.sh {{harness}} --{{scope}} --project {{project}} --uninstall

# Everything CI runs, locally.
check: validate drift test
    @echo "all checks passed"
