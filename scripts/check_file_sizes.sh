#!/usr/bin/env bash
# ── File Size Governance Check ──────────────────────────────────────────
# Fails CI if any source file exceeds the hard size limits.
#
# Hard limits (from pyproject.toml [tool.archolith.size-limits]):
#   Business logic: 500 LOC
#   Test files:    1200 LOC
#   Config files:   200 LOC
#   __init__.py:    100 LOC
#
# Usage:  bash scripts/check_file_sizes.sh
#         (exit code 0 = clean, 1 = violations found)
# ────────────────────────────────────────────────────────────────────────

set -o errexit
set -o nounset
set -o pipefail

HARD_LIMIT_PY=500
HARD_LIMIT_TEST=1200
HARD_LIMIT_CONFIG=200
HARD_LIMIT_INIT=100

EXCLUDE_DIRS=".venv .benchmarks .pytest_cache __pycache__"
EXCLUDE_PATTERNS=""

# Build find exclusion args
FIND_EXCLUDES=()
for d in $EXCLUDE_DIRS; do
    FIND_EXCLUDES+=( -not -path "*/$d/*" )
done

EXIT_CODE=0

check_limit() {
    local file="$1"
    local limit="$2"
    local category="$3"
    local lines
    lines=$(wc -l < "$file")
    if [ "$lines" -gt "$limit" ]; then
        echo "FAIL  $category  $lines LOC  $file  (limit: $limit)"
        EXIT_CODE=1
    fi
}

# 1. Business logic files (*.py in archolith_proxy/, excluding __init__.py)
while IFS= read -r -d '' f; do
    check_limit "$f" $HARD_LIMIT_PY "logic"
done < <(find archolith_proxy -name '*.py' "${FIND_EXCLUDES[@]}" -not -name '__init__.py' -print0 2>/dev/null)

# 2. Test files (*.py in tests/)
while IFS= read -r -d '' f; do
    check_limit "$f" $HARD_LIMIT_TEST "test"
done < <(find tests -name '*.py' "${FIND_EXCLUDES[@]}" -print0 2>/dev/null)

# 3. Config files
while IFS= read -r -d '' f; do
    check_limit "$f" $HARD_LIMIT_CONFIG "config"
done < <(find . -maxdepth 1 -name '*.py' \( -name 'config.py' -o -name 'settings.py' -o -name 'pyproject.toml' \) -print0 2>/dev/null)

# 4. __init__.py files
while IFS= read -r -d '' f; do
    check_limit "$f" $HARD_LIMIT_INIT "init"
done < <(find archolith_proxy -name '__init__.py' "${FIND_EXCLUDES[@]}" -print0 2>/dev/null)

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "OK — all files within size limits."
fi
exit "$EXIT_CODE"
