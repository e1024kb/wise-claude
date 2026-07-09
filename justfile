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

# Regenerate all generated port content in place (from core/ + Claude skills + core/ports/).
build:
    python3 scripts/build_ports.py

# Verify committed generated content matches a fresh render (exits non-zero on drift).
build-check:
    python3 scripts/build_ports.py --check

# Install a harness port. Examples:
#   just install claude
#   just install codex
#   just install cursor
#   just install hermes
#   just install cursor project ./my-repo
install harness scope="user" project=".":
    ./install.sh {{harness}} {{ if scope == "project" { "--project " + project } else { "--user" } }}

# Uninstall a harness port (removes exactly what install added).
uninstall harness scope="user" project=".":
    ./install.sh {{harness}} {{ if scope == "project" { "--project " + project } else { "--user" } }} --uninstall

# Copy-install smoke test into throwaway HOME/data dirs.
smoke:
    bash scripts/install_smoke.sh

# Everything CI runs, locally.
check: validate build-check test smoke
    @echo "all checks passed"
