#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
DESTINATION=${1:?Usage: sh scripts/build-native-web.sh /absolute/external/new/bundle-directory}
case "$DESTINATION" in
    /*) ;;
    *) printf '%s\n' 'Bundle path must be absolute' >&2; exit 1 ;;
esac
DESTINATION=$("${PYTHON:-python3}" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DESTINATION")
case "$DESTINATION/" in
    "$ROOT/"*) printf '%s\n' 'Bundle must be outside the checkout' >&2; exit 1 ;;
esac
if [ -e "$DESTINATION" ]; then
    printf '%s\n' 'Use a new bundle path; existing directories are not overwritten' >&2
    exit 1
fi
MODULES='__init__ browser brokered_browser documents gateway http_fetch lambda_handler quota search url_policy native_handler'
for MODULE in $MODULES; do
    test -f "$ROOT/web_capabilities/$MODULE.py" || { printf 'Missing source module: %s\n' "$MODULE" >&2; exit 1; }
done
SOURCE_COMMIT=$(git -C "$ROOT" rev-parse HEAD)
mkdir -p "$(dirname -- "$DESTINATION")"
mkdir "$DESTINATION"
cp "$ROOT/web_capabilities/requirements-lambda.txt" "$DESTINATION/requirements-lambda.txt"
if [ -n "${UV:-}" ]; then
    "$UV" pip install --python-version 3.12 --python-platform aarch64-manylinux_2_28 \
        --only-binary=:all: --require-hashes --target "$DESTINATION" \
        -r "$DESTINATION/requirements-lambda.txt"
else
    "${PYTHON:-python3}" -m pip install --no-compile --only-binary=:all: --require-hashes \
        --python-version 3.12 --implementation cp --abi cp312 \
        --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 \
        --target "$DESTINATION" -r "$DESTINATION/requirements-lambda.txt"
fi
mkdir "$DESTINATION/web_capabilities"
for MODULE in $MODULES; do
    cp "$ROOT/web_capabilities/$MODULE.py" "$DESTINATION/web_capabilities/"
done
printf '%s\n' "$SOURCE_COMMIT" > "$DESTINATION/source-commit.txt"
(
    cd "$DESTINATION"
    { find web_capabilities -type f -name '*.py' -print; printf '%s\n' requirements-lambda.txt source-commit.txt; } \
        | LC_ALL=C sort | xargs sha256sum
) > "$DESTINATION/source-sha256.txt"
chmod -R a-w "$DESTINATION"
printf '%s\n' "$DESTINATION"
