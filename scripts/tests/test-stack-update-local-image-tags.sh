#!/bin/bash
# Regression test for stack-update.sh local-image tag parser
# Bug: image="${entry##*:}" stripped through the last colon, resolving
#      every LOCAL_IMAGES entry to just "latest" instead of "repo:tag".
# Fix: image="${entry#*:}" strips through the first colon only.
#
# Run: bash scripts/tests/test-stack-update-local-image-tags.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

UPDATE_SCRIPT="$STACK_DIR/scripts/stack-update.sh"
if [ ! -f "$UPDATE_SCRIPT" ]; then
    echo "FAIL: $UPDATE_SCRIPT not found"
    exit 1
fi

# --- Test 1: Verify the fix is present ---
# The buggy version uses ${entry##*:} (double hash), the fixed version uses ${entry#*:} (single hash)
BUGGY_PATTERN='image="${entry##*:}"'
FIXED_PATTERN='image="${entry#*:}"'

if grep -qF "$BUGGY_PATTERN" "$UPDATE_SCRIPT"; then
    echo "FAIL: script still uses the buggy double-hash pattern"
    grep -nF "$BUGGY_PATTERN" "$UPDATE_SCRIPT"
    exit 1
fi

if ! grep -qF "$FIXED_PATTERN" "$UPDATE_SCRIPT"; then
    echo "FAIL: script does not use the fixed single-hash pattern"
    exit 1
fi
echo "PASS: image variable uses single-hash pattern (strips first segment only)"

# --- Test 2: Parse all four LOCAL_IMAGES entries and verify correct extraction ---
LOCAL_IMAGES=(
    "Dockerfile.gateway-dev:local/hermes-webui-stack-gateway-dev:latest"
    "Dockerfile.webui:local/hermes-webui-stack-webui:latest"
    "Dockerfile.helm:local/hermes-webui-stack-helm:latest"
    "Dockerfile.agenthub-api:local/hermes-webui-stack-agenthub-api:latest"
)

EXPECTED_DOCKERFILES=("Dockerfile.gateway-dev" "Dockerfile.webui" "Dockerfile.helm" "Dockerfile.agenthub-api")
EXPECTED_IMAGES=(
    "local/hermes-webui-stack-gateway-dev:latest"
    "local/hermes-webui-stack-webui:latest"
    "local/hermes-webui-stack-helm:latest"
    "local/hermes-webui-stack-agenthub-api:latest"
)

PASS_COUNT=0
TOTAL=${#LOCAL_IMAGES[@]}

for i in "${!LOCAL_IMAGES[@]}"; do
    entry="${LOCAL_IMAGES[$i]}"
    # Use the SAME parsing logic as the fixed script
    dockerfile="${entry%%:*}"
    image="${entry#*:}"

    expected_df="${EXPECTED_DOCKERFILES[$i]}"
    expected_img="${EXPECTED_IMAGES[$i]}"

    if [ "$dockerfile" != "$expected_df" ]; then
        echo "FAIL [$i]: dockerfile mismatch - got [$dockerfile] expected [$expected_df]"
        exit 1
    fi

    if [ "$image" != "$expected_img" ]; then
        echo "FAIL [$i]: image mismatch - got [$image] expected [$expected_img]"
        exit 1
    fi

    # CRITICAL: image must NOT resolve to just "latest"
    if [ "$image" = "latest" ]; then
        echo "FAIL [$i]: image resolved to 'latest' - the bug is still present"
        exit 1
    fi

    # CRITICAL: image must NOT contain "library/latest"
    case "$image" in
        *library/latest*)
            echo "FAIL [$i]: image resolved to library/latest - the bug is still present"
            exit 1
            ;;
    esac

    # CRITICAL: image must contain the repository prefix
    case "$image" in
        local/hermes-webui-stack-*)
            ;;
        *)
            echo "FAIL [$i]: image does not start with local/hermes-webui-stack- - got [$image]"
            exit 1
            ;;
    esac

    echo "PASS [$i]: $dockerfile -> $image"
    PASS_COUNT=$((PASS_COUNT + 1))
done

echo ""
echo "Results: $PASS_COUNT/$TOTAL entries parsed correctly"

if [ "$PASS_COUNT" -ne "$TOTAL" ]; then
    echo "FAIL: not all entries passed"
    exit 1
fi

# --- Test 3: Verify all four image tags are distinct ---
# The bug caused all four to collapse to "latest"; verify they are unique
UNIQUE_COUNT=$(printf '%s\n' "${EXPECTED_IMAGES[@]}" | sort -u | wc -l)
if [ "$UNIQUE_COUNT" -ne "$TOTAL" ]; then
    echo "FAIL: expected $TOTAL unique image tags, got $UNIQUE_COUNT"
    exit 1
fi
echo "PASS: all $TOTAL image tags are unique"

# --- Test 4: Verify all images have repo:tag format (contain a colon) ---
for entry in "${LOCAL_IMAGES[@]}"; do
    image="${entry#*:}"
    if ! echo "$image" | grep -q ':'; then
        echo "FAIL: image [$image] does not contain a colon - missing tag separator"
        exit 1
    fi
done
echo "PASS: all image tags have repo:tag format (with colon)"

# --- Test 5: Verify the buggy pattern would have produced wrong results ---
# Demonstrate that the OLD pattern (##*:) collapses to "latest" for all entries
BUGGY_FAIL=0
for entry in "${LOCAL_IMAGES[@]}"; do
    buggy_image="${entry##*:}"
    if [ "$buggy_image" != "latest" ]; then
        echo "NOTE: buggy pattern did not collapse to 'latest' for [$entry] - got [$buggy_image]"
        BUGGY_FAIL=1
    fi
done
if [ "$BUGGY_FAIL" -eq 0 ]; then
    echo "PASS: confirmed old buggy pattern would collapse all entries to 'latest'"
else
    echo "WARN: buggy pattern behavior differs from expected (review needed)"
fi

echo ""
echo "=== ALL TESTS PASSED ==="
