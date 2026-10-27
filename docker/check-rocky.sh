#!/usr/bin/env bash
# Build locally, run without external networking, retain each run's evidence.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
image=${1:-goit-cs-hw-05-rocky-tests}
if [ "$(id -u)" -eq 0 ]; then
    echo 'Запустіть make check-rocky від звичайного користувача, без sudo.' >&2
    exit 1
fi
mkdir -p artifacts/rocky
run_dir=$(mktemp -d "$PWD/artifacts/rocky/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
trap 'status=$?; printf "%s\n" "$status" > "$run_dir/exit-code.txt"; printf "Артефакти Rocky: %s\n" "$run_dir"' EXIT

make --no-print-directory build-rocky-tests ROCKY_TEST_IMAGE="$image" 2>&1 \
    | tee "$run_dir/build.log"
docker image inspect "$image" > "$run_dir/image.json"
image_id=$(docker image inspect --format '{{.Id}}' "$image")
docker version --format '{{json .}}' > "$run_dir/docker-version.json"

# Only this fresh result directory is shared. Tests use the Linux /tmp filesystem.
docker run --rm --init --network none --read-only \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,size=536870912 \
    --volume "$run_dir:/results:Z" \
    --env "HW05_TEST_IMAGE_ID=$image_id" \
    "$image_id" 2>&1 | tee "$run_dir/check.log"
