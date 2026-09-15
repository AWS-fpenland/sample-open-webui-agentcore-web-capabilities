#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DESTINATION=${1:?Usage: build-web-canary.sh /absolute/external/empty/bundle-directory}
case "$DESTINATION" in
    /*) ;;
    *) printf '%s\n' 'Bundle path must be absolute' >&2; exit 1 ;;
esac
case "$DESTINATION/" in
    "$ROOT/"*) printf '%s\n' 'Bundle must be outside the checkout' >&2; exit 1 ;;
esac
if [ -e "$DESTINATION" ]; then
    printf '%s\n' 'Use a new empty bundle path; existing directories are not overwritten' >&2
    exit 1
fi
mkdir -p "$DESTINATION"
if [ -n "${UV:-}" ]; then
    "$UV" pip install --python-version 3.12 --python-platform aarch64-manylinux_2_28 \
        --only-binary=:all: --require-hashes --target "$DESTINATION" \
        -r "$ROOT/web_capabilities/requirements-lambda.txt"
else
    "${PYTHON:-python3}" -m pip install --no-compile --only-binary=:all: --require-hashes \
        --python-version 3.12 --implementation cp --abi cp312 \
        --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 \
        --target "$DESTINATION" -r "$ROOT/web_capabilities/requirements-lambda.txt"
fi
mkdir "$DESTINATION/web_capabilities"
for MODULE in __init__ browser brokered_browser documents gateway http_fetch lambda_handler quota search url_policy; do
    cp "$ROOT/web_capabilities/$MODULE.py" "$DESTINATION/web_capabilities/"
done
cp "$ROOT/web_capabilities/requirements-lambda.txt" "$DESTINATION/requirements-lambda.txt"
git -C "$ROOT" rev-parse HEAD > "$DESTINATION/source-commit.txt"
(cd "$DESTINATION" && find web_capabilities -type f -name '*.py' -print | sort | xargs sha256sum) > "$DESTINATION/source-sha256.txt"
printf '%s\n' "$DESTINATION"
