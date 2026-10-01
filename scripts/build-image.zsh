#!/usr/bin/env zsh
# build-image.zsh — put the pinned template's images into the department's registry.
#
#   scripts/build-image.zsh --config <dept> [--lock <template.lock>] [--allow-unpinned-template]
#
# * chat:<ref>-<sha7>  built by ACR Tasks (az acr build) from image/ of THIS template
#   checkout, which must be exactly template.lock's commit (clean). Built once: a tag
#   that exists is reused, and is locked against overwrite and deletion.
# * the sidecar images in image/sidecars.json, imported from Docker Hub once by their
#   pinned digest (tagged <repo>:<tag> here, and locked), so no server depends on Docker
#   Hub (or its rate limits, or a moved upstream tag) at run time.
# Prints the image reference (<registry>/chat@sha256:...) on stdout, and writes
# image=... to $GITHUB_OUTPUT in Actions.
source "${0:A:h}/common.zsh"
LOCK="" ALLOW_UNPINNED=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --lock) LOCK="${2:?}"; shift 2 ;;
    --allow-unpinned-template) ALLOW_UNPINNED=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
az_login
LOCK="${LOCK:-$CONFIG_DIR/../template.lock}"
[[ -f "$LOCK" ]] || die "no template.lock at $LOCK (pass --lock)"
SHA="$(jq -er .sha "$LOCK")" || die "$LOCK has no commit sha"
REF="$(jq -er .ref "$LOCK")"
[[ "$SHA" =~ '^[0-9a-f]{40}$' ]] || die "$LOCK: sha '$SHA' is not a commit"

# The image is built from this checkout, so it must be the locked commit, unmodified.
HEAD="$(git -C "$CHAT_ROOT" rev-parse HEAD 2>/dev/null || true)"
DIRTY="$(git -C "$CHAT_ROOT" status --porcelain -- image 2>/dev/null || true)"
if [[ "$HEAD" != "$SHA" || -n "$DIRTY" ]]; then
  $ALLOW_UNPINNED || die "template checkout is ${HEAD:-not a git checkout}${DIRTY:+ with changes in image/}, template.lock pins $SHA; check out that commit (or --allow-unpinned-template for local tests)"
  log_warn "building from an unpinned template checkout (--allow-unpinned-template)"
fi

ACR="$(jqp .names.registry)"
LOGIN="$(az acr show -n "$ACR" -g "$RG" --query loginServer -o tsv)"
[[ -n "$LOGIN" ]] || die "registry $ACR not found; run deploy-platform.zsh first"
TAG="chat:${REF}-${SHA[1,7]}"

digest_of() { az acr repository show -n "$ACR" --image "$1" --query digest -o tsv 2>/dev/null || true; }
lock_tag() { az acr repository update -n "$ACR" --image "$1" --write-enabled false --delete-enabled false --output none; }

log_step "Zulip image $LOGIN/$TAG"
DIGEST="$(digest_of "$TAG")"
if [[ -n "$DIGEST" ]]; then
  log_ok "already built ($DIGEST)"
else
  log_info "building with ACR Tasks from $CHAT_ROOT/image (commit ${SHA[1,7]})"
  az acr build -r "$ACR" -g "$RG" -t "$TAG" --platform linux/amd64 \
    --build-arg "CHAT_TEMPLATE_SHA=$SHA" "$CHAT_ROOT/image" >&2 \
    || die "az acr build failed"
  DIGEST="$(digest_of "$TAG")"
  [[ "$DIGEST" == sha256:* ]] || die "built $TAG but the registry reports no digest"
  log_ok "built ($DIGEST)"
fi
lock_tag "$TAG"

log_step "Sidecar images"
# Each is pinned as docker.io/<repo>:<tag>@sha256:<multi-arch digest>. The import is by
# that digest (az acr import --source <repo>@sha256:...), tagged <repo>:<tag> in the
# registry, so a moved upstream tag can never change what a server runs.
for src in "${(@f)$(jq -r 'to_entries[] | select(.key | startswith("_") | not) | .value' "$CHAT_ROOT/image/sidecars.json")}"; do
  [[ "$src" =~ '^docker\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$' ]] \
    || die "image/sidecars.json: '$src' is not pinned as docker.io/<repo>:<tag>@sha256:<digest>"
  pinned="${src##*@}"          # sha256:...
  ref="${src%@*}"              # docker.io/<repo>:<tag>
  target="${ref#docker.io/}"   # <repo>:<tag> in the department registry
  repo="${target%:*}"
  have="$(digest_of "$target")"
  if [[ "$have" == "$pinned" ]]; then
    log_ok "$target present ($pinned)"
  elif [[ -z "$have" ]]; then
    az_retry acr import -n "$ACR" -g "$RG" --source "docker.io/$repo@$pinned" --image "$target" --output none
    have="$(digest_of "$target")"
    [[ "$have" == "$pinned" ]] || die "imported $target but the registry reports ${have:-no digest}, not the pinned $pinned"
    log_ok "imported $target ($pinned)"
  else
    die "$target in $ACR is $have, but image/sidecars.json pins $pinned; registry tags are locked, so pin a new tag"
  fi
  lock_tag "$target"
done

IMAGE="$LOGIN/chat@$DIGEST"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then print -r -- "image=$IMAGE" >> "$GITHUB_OUTPUT"; fi
summary "- image \`$IMAGE\` ($TAG, template ${SHA[1,7]})"
print -r -- "$IMAGE"
