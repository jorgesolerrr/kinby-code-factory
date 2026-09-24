#!/bin/sh
# Build the coder image the way the hub prepares it: kinby's Dockerfile, then this
# package's recipe, then the pinned install of one commit of this repository.
#   scripts/build-image.sh <kinby-checkout> <factory-commit-sha> <image-tag>
set -eu

kinby="$1"
commit="$2"
tag="$3"
here="$(cd "$(dirname "$0")/.." && pwd)"
dockerfile="$(mktemp)"
trap 'rm -f "$dockerfile"' EXIT

{
    cat "$kinby/Dockerfile"
    echo
    cat "$here/image/recipe.Dockerfile"
    printf 'RUN ["uv", "pip", "install", "--system", "--no-cache", "--no-sources", "git+https://github.com/jorgesolerrr/kinby-code-factory@%s"]\n' "$commit"
} >"$dockerfile"
docker build --file "$dockerfile" --tag "$tag" "$kinby"
