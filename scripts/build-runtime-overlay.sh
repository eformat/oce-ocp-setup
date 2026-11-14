#!/usr/bin/env bash
# Build and publish a sandbox runtime image that extends the currently deployed
# oce-runtime image with the OpenClaw dist patch from
# manifests/openshift/runtime-overlay/Containerfile.template. The runtime image
# serves both the Agent gateway and the sandbox agent container (installation
# images.gateway / images.agent), so one overlay covers both roles.
#
# The patched file is extracted from the deployed base image, patched, and
# COPYed back with its original ownership, because the dist tree is root-owned
# while the image runs as uid 1000.
#
# After publishing, point installation.yaml's images.gateway and images.agent
# at the new digest, run prepare-inputs refresh, and helm upgrade the release.
#
# Prerequisites: podman and cluster read access to discover the deployed
# runtime image.
set -euo pipefail

usage() {
  echo "usage: scripts/build-runtime-overlay.sh <tag>" >&2
  echo "  example: scripts/build-runtime-overlay.sh d80baa049-code-mode-off" >&2
}

[[ $# -eq 1 ]] || { usage; exit 1; }
tag=$1
repository=quay.io/eformat/oce-runtime
repo_root=$(cd "$(dirname "$0")/.." && pwd)
overlay_dir="$repo_root/manifests/openshift/runtime-overlay"
template="$overlay_dir/Containerfile.template"

# The overlay base must be the digest the cluster actually runs, so the patch
# is the only delta.
export KUBECONFIG=${KUBECONFIG:-"$HOME/.kube/config"}
base_image=$(kubectl -n oce-d29cae08ac8241e get pod \
  -l openclaw.dev/workload-role=gateway -o jsonpath='{.items[0].spec.containers[0].image}')
[[ $base_image == *@sha256:* ]] || {
  echo "deployed runtime image is not digest-pinned: $base_image" >&2
  exit 1
}
echo "base: $base_image"

workdir=$(mktemp -d /tmp/opencode/runtime-overlay.XXXXXX)
trap 'rm -rf "$workdir"' EXIT

# Extract the thread-config dist file from the base image and apply the patch.
dist_name=$(podman run --rm --entrypoint /bin/sh "$base_image" \
  -c 'ls /app/node_modules/openclaw/dist/thread-requests-*.mjs' | head -1 | xargs -n1 basename)
[[ $dist_name == thread-requests-*.mjs ]] || {
  echo "could not locate the thread-requests dist file in $base_image" >&2
  exit 1
}
podman run --rm --entrypoint /bin/sh "$base_image" \
  -c 'cat /app/node_modules/openclaw/dist/'"$dist_name" > "$workdir/$dist_name"
patched_count=$(grep -c '"features.code_mode": true' "$workdir/$dist_name" || true)
[[ $patched_count == 1 ]] || {
  echo "expected exactly one code-mode enablement in $dist_name, found: $patched_count" >&2
  exit 1
}
sed -i 's/"features.code_mode": true,/"features.code_mode": false,/' "$workdir/$dist_name"

# Render the Containerfile with the discovered base image and file name, then
# build with the patched file as the only context entry.
sed "s|^FROM .*|FROM $base_image|; s|thread-requests.patched.mjs|$dist_name|; s|/app/node_modules/openclaw/dist/thread-requests-[^ ]*\.mjs|/app/node_modules/openclaw/dist/$dist_name|" \
  "$template" > "$workdir/Containerfile"
podman build -f "$workdir/Containerfile" -t "$repository:$tag" "$workdir"
podman push "$repository:$tag"
digest=$(skopeo inspect "docker://$repository:$tag" --format '{{.Digest}}')
echo "published: $repository@sha256:$digest"
echo "set installation.yaml images.gateway and images.agent to $repository@sha256:$digest, then:"
echo "  deploy/rhoai/scripts/prepare-inputs refresh  (from the OCE checkout)"
echo "  helm upgrade oce <chart> --namespace openclaw-system -f <values.yaml> --wait"
