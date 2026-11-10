#!/bin/bash
set -euo pipefail
umask 022

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"
report_dir=${1:-work/rapl-fixture}
mkdir -p "$report_dir"
report_dir=$(cd "$report_dir" && pwd)
context_dir=$(mktemp -d "${TMPDIR:-/tmp}/wattch-rapl-context.XXXXXX")
export BUILDX_CONFIG="${BUILDX_CONFIG:-$context_dir/buildx}"
container_id=
cleanup() {
    if [[ -n "$container_id" ]]; then
        docker rm -f "$container_id" >/dev/null
    fi
    rm -rf "$context_dir"
}
trap cleanup EXIT

cargo build --locked --release -p wattch-cli -p wattch-powerd
target_dir=$(cargo metadata --format-version 1 --no-deps | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')
mkdir -p "$context_dir/bin"
install -m 0755 "$target_dir/release/wattch" "$target_dir/release/wattch-powerd" "$context_dir/bin/"
cp -a clients/python "$context_dir/python"
cp packaging/rapl-fixture/test_fixture.py "$context_dir/test_fixture.py"
cp packaging/rapl-fixture/Dockerfile "$context_dir/Dockerfile"
docker build -t wattch-rapl-fixture:test "$context_dir"
container_id=$(docker create --network none wattch-rapl-fixture:test)
run_status=0
docker start --attach "$container_id" || run_status=$?
docker cp "$container_id":/tmp/rapl-fixture-report.json "$report_dir/report.json"
printf 'Fixture report: %s/report.json\n' "$report_dir"
exit "$run_status"
