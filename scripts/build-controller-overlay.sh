#!/usr/bin/env bash
# Build and publish a sandbox controller image that extends the currently
# deployed aipcc controller image with the local OpenClaw Enterprise checkout's
# changed controller sources. This is the sandbox delivery path for controller
# changes: the published aipcc image only follows the upstream release, so
# controller fixes land in the cluster by extending its digest and pushing to
# the operator's quay namespace, then repointing the Helm release.
#
# The Containerfile under manifests/openshift/controller-overlay/ lists the
# copied files explicitly; update it whenever the overlay must carry more.
#
# Prerequisites: podman, an existing quay.io login for the target namespace
# (podman login quay.io), and cluster read access to discover the deployed
# base image.
set -euo pipefail

usage() {
  echo "usage: scripts/build-controller-overlay.sh <source-checkout> <tag>" >&2
  echo "  example: scripts/build-controller-overlay.sh ~/git/openclaw-enterprise 4292eb22-glm-catalog" >&2
}

[[ $# -eq 2 ]] || { usage; exit 1; }
checkout=$1
tag=$2
repository=quay.io/eformat/oce-controller
repo_root=$(cd "$(dirname "$0")/.." && pwd)
containerfile="$repo_root/manifests/openshift/controller-overlay/Containerfile"

# The overlay base must be the digest the cluster actually runs, so the copied
# sources are the only delta.
export KUBECONFIG=${KUBECONFIG:-"$HOME/.kube/config"}
base_image=$(kubectl -n openclaw-system get deployment openclaw-enterprise-worker \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ $base_image == *@sha256:* ]] || {
  echo "deployed controller image is not digest-pinned: $base_image" >&2
  exit 1
}
echo "base: $base_image"

sed "s|^FROM .*|FROM $base_image|" "$containerfile" > "$(dirname "$containerfile")/Containerfile.rendered"
podman build -f "$(dirname "$containerfile")/Containerfile.rendered" \
  -t "$repository:$tag" "$checkout"
podman push "$repository:$tag"
digest=$(skopeo inspect "docker://$repository:$tag" --format '{{.Digest}}')
echo "published: $repository@sha256:$digest"
echo "set images.controller to $repository@sha256:$digest in the private values.yaml, then:"
echo "  helm upgrade oce <chart> --namespace openclaw-system -f <values.yaml> --wait"
