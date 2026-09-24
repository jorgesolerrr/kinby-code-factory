#!/usr/bin/env bash
# Rehearse docs/migration.md on disposable copies, on the box that runs the coder.
#
#   scripts/rehearse-migration.sh <kinby-checkout> <factory-sha> <live-coder-dir> <old-image>
#
# <kinby-checkout> is checked out at the kinby commit the hub will build. The script copies
# the live coder's directory with dummy secrets, runs it on <old-image> the way Compose did,
# migrates it, adopts it into a private hub with its package, and updates it to
# <factory-sha> through that hub. Every container, volume, network and file it creates
# carries one prefix and is removed on exit. The live coder is only read.
set -euo pipefail

kinby="$(realpath "$1")"
factory_sha="$2"
live="$(realpath "$3")"
old_image="$4"
here="$(cd "$(dirname "$0")/.." && pwd)"
kinby_sha="$(git -C "$kinby" rev-parse HEAD)"
prefix="kinby-rehearsal-$$"
network="$prefix"
work="$(mktemp -d "$HOME/$prefix.XXXX")"
hub_dir="$work/hub"
url="ws://127.0.0.1:8080/ws"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'REHEARSAL FAILED: %s\n' "$*" >&2; exit 1; }
as_root() { docker run --rm --entrypoint sh --mount "type=bind,src=$work,dst=/work" "$old_image" -c "$1"; }

cleanup() {
    step "Removing everything the rehearsal created"
    docker ps -aq --filter "network=$network" | xargs -r docker rm -f >/dev/null
    docker rm -f "$prefix-hub" "$prefix-coder" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    docker volume rm "$prefix-workspace" "$prefix-codex" >/dev/null 2>&1 || true
    docker image rm "$prefix-hub" "$prefix-package" >/dev/null 2>&1 || true
    as_root 'rm -rf /work/hub' || true
    rm -rf "$work"
}
trap cleanup EXIT

[ -z "$(git -C "$kinby" status --porcelain)" ] || fail "$kinby has local changes"

step "Copying the live coder with dummy secrets"
mkdir -p "$hub_dir/coder"
docker run --rm --entrypoint sh \
    --mount "type=bind,src=$live,dst=/from,readonly" \
    --mount "type=bind,src=$hub_dir/coder,dst=/to" \
    "$old_image" -c 'cd /from && tar --exclude=./.env --exclude="./recovery-*" -cf - . | tar -xf - -C /to'
cat >"$work/dummy.env" <<'EOF'
GH_TOKEN=rehearsal-not-a-token
GITHUB_WEBHOOK_SECRET=rehearsal
ANTHROPIC_API_KEY=rehearsal
CLAUDE_CODE_OAUTH_TOKEN=rehearsal
GIT_USER_NAME=rehearsal
GIT_USER_EMAIL=rehearsal@example.invalid
EOF
as_root "cp /work/dummy.env /work/hub/coder/.env && chmod 600 /work/hub/coder/.env"
fingerprint() {
    as_root "cd /work/hub/coder && find . -path ./workspace -prune -o -path ./.state -prune \
        -o -type f ! -name .env ! -name kinby.toml ! -name package.yaml \
        ! -path './routines/*' -print | sort | xargs sha256sum"
}
before="$(fingerprint)"

step "Running the copy on the old image, as Compose did"
docker network create "$network" >/dev/null
coder_mounts=(
    --mount "type=bind,src=$hub_dir/coder,dst=/instance"
    --mount "type=volume,src=$prefix-workspace,dst=/instance/workspace"
    --mount "type=volume,src=$prefix-codex,dst=/root/.codex"
)
healthy() {
    for _ in $(seq 60); do
        docker exec "$1" curl -fsS http://127.0.0.1:8787/health >/dev/null 2>&1 && return 0
        sleep 2
    done
    docker logs --tail 40 "$1" >&2
    fail "$1 never answered /health"
}
docker run --detach --name "$prefix-coder" --network "$network" --env-file "$work/dummy.env" \
    "${coder_mounts[@]}" "$old_image" serve >/dev/null
healthy "$prefix-coder"
docker rm -f "$prefix-coder" >/dev/null

step "Building the package image at $factory_sha on kinby $kinby_sha"
"$here/scripts/build-image.sh" "$kinby" "$factory_sha" "$prefix-package" >"$work/build.log" 2>&1 \
    || { tail -40 "$work/build.log" >&2; fail "the package image did not build"; }

step "Migrating the copy's configuration"
docker run --rm --entrypoint python --mount "type=bind,src=$hub_dir/coder,dst=/instance" \
    "$prefix-package" -m kinby_code_factory.migrate /instance
as_root 'cat /work/hub/coder/package.yaml' | grep -v '^ *#'

step "Running the copy on the package image"
docker run --detach --name "$prefix-coder" --network "$network" --env-file "$work/dummy.env" \
    "${coder_mounts[@]}" "$prefix-package" serve >/dev/null
healthy "$prefix-coder"

step "Starting a private hub on kinby $kinby_sha"
docker build --quiet --tag "$prefix-hub" "$kinby" >/dev/null
docker run --detach --name "$prefix-hub" --network "$network" \
    --mount "type=bind,src=$hub_dir,dst=/hub" \
    --mount "type=bind,src=$kinby,dst=/source,readonly" \
    --mount "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" \
    --entrypoint kinby "$prefix-hub" hub /hub --docker-host-directory="$hub_dir" \
    --source=/source --network="$network" --listen=0.0.0.0:8080 >/dev/null
for _ in $(seq 30); do
    access="$(docker logs "$prefix-hub" 2>&1 | sed -n 's/^access token: //p')"
    [ -n "$access" ] && break
    sleep 1
done
[ -n "$access" ] || { docker logs "$prefix-hub" >&2; fail "the hub printed no access token"; }

step "Adopting the copy with its package"
python3 - "$here/image/recipe.Dockerfile" "$factory_sha" >"$work/coder-package.json" <<'EOF'
import json
import sys
from pathlib import Path

recipe, sha = sys.argv[1:]
print(json.dumps({
    "id": "coder",
    "distribution": "kinby-code-factory",
    "version": {"url": "https://github.com/jorgesolerrr/kinby-code-factory", "sha": sha},
    "image_recipe": Path(recipe).read_text(encoding="utf-8"),
}))
EOF
as_root "cp /work/coder-package.json /work/hub/coder-package.json"
hub() { docker exec --env "KINBY_TOKEN=$1" "$prefix-hub" kinby hub "${@:2}"; }
adopt=(adopt --connect "$url" /hub/coder "$prefix-coder" --package /hub/coder-package.json)
hub "$access" "${adopt[@]}" --preview --relinquished --claim-signals >"$work/preview.json" \
    || { cat "$work/preview.json"; fail "the preview found a blocking finding"; }
instance_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["instance_id"])' "$work/preview.json")"
hub "$access" "${adopt[@]}" --relinquished --claim-signals

step "Updating the adopted copy to the package commit through the hub"
update_token="$(docker exec "$prefix-hub" kinby hub /hub update-token rotate | sed -n 's/^update token: //p')"
hub "$update_token" update --connect "$url" "$instance_id" --revision "$kinby_sha" \
    --package coder --package-commit "$factory_sha"

step "Checking what the hub runs now"
container="$(docker ps -q --filter "network=$network" --filter "volume=$prefix-workspace")"
[ -n "$container" ] || fail "no container runs the adopted copy"
docker exec "$container" python -m kinby.packages coder /instance >/dev/null
docker exec "$container" python - <<'EOF'
from pathlib import Path

from kinby.instance import load_instance
from kinby.plugins.routines import load_routines

instance = load_instance(Path("/instance"))
assert instance.manifest.id == "coder", instance.manifest.id
assert instance.manifest.package is not None and instance.manifest.package.id == "coder"
routines, warnings = load_routines(instance)
assert warnings == (), warnings
found = {routine.name: (routine.enabled, routine.arguments) for routine in routines}
expected = {name: (True, {}) for name in ("implement-ready-issue", "babysit-pull-request")}
assert found == expected, found
print("routines:", found)
EOF
[ "$(fingerprint)" = "$before" ] || fail "files outside the routines, kinby.toml and package.yaml changed"
docker exec "$container" test -d /instance/workspace/.git || fail "the workspace volume is not attached"

step "Rehearsal passed"
