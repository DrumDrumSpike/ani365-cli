#!/usr/bin/env bash
# Update only this application's image; secrets and SQLite remain in their volume.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
umask 077
exec 9>.update.lock
flock -n 9 || exit 0

for dependency in curl jq docker; do
  command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; exit 1; }
done

repository=DrumDrumSpike/ani365-cli
image=ghcr.io/drumdrumspike/ani365-bot
release=$(curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
  -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$repository/releases?per_page=100" | jq -r \
  '[.[] | select(.draft == false and .prerelease == false) | select(.tag_name | test("^bot-v[0-9]+\\.[0-9]+\\.[0-9]+$"))] | sort_by(.published_at) | last | .tag_name // empty')
if [[ ! "$release" =~ ^bot-v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "No stable bot-vX.Y.Z release found; no update."
  exit 0
fi

if [[ ! -f deploy.env ]]; then
  printf '%s\n' 'BOT_IMAGE=ani365-bot:local' > deploy.env
fi
compose=(docker compose --env-file deploy.env)

# Pull before draining: failed publication/network leaves the running bot untouched.
docker pull "$image:$release"
candidate=$(docker image inspect "$image:$release" --format '{{index .RepoDigests 0}}')
[[ "$candidate" == "$image@sha256:"* ]] || { echo "Unexpected image digest" >&2; exit 1; }
if [[ "$(sed -n 's/^BOT_IMAGE=//p' deploy.env)" == "$candidate" ]]; then
  echo "Already on $release."
  exit 0
fi
previous_id=$("${compose[@]}" images -q bot)
[[ -n "$previous_id" ]] || { echo "Start the bot before enabling updates." >&2; exit 1; }
# Preserve the actually running image even when it was built locally.
docker tag "$previous_id" ani365-bot:rollback

resume() {
  "${compose[@]}" exec -T bot python -m ani365_bot.control resume >/dev/null 2>&1 || true
}
trap resume EXIT
"${compose[@]}" exec -T bot python -m ani365_bot.control drain
echo "Waiting for the current menu to finish or expire (up to 20 minutes)."
ready=false
for ((attempt=0; attempt<240; attempt++)); do
  if "${compose[@]}" exec -T bot python -m ani365_bot.control ready; then
    ready=true
    break
  fi
  sleep 5
done
if [[ "$ready" != true ]]; then
  echo "Bot is still busy or unavailable; update postponed." >&2
  exit 1
fi

cp deploy.env deploy.env.previous
printf 'BOT_IMAGE=%s\n' "$candidate" > deploy.env.next
mv deploy.env.next deploy.env
if ! "${compose[@]}" up -d --no-build --wait --wait-timeout 240 bot; then
  echo "New version failed its health check; rolling back." >&2
  printf '%s\n' 'BOT_IMAGE=ani365-bot:rollback' > deploy.env
  "${compose[@]}" up -d --no-build --wait --wait-timeout 240 bot
  exit 1
fi
echo "Installed $release ($candidate)."

# Remove only unused older images of this bot, preserving current + rollback.
current_id=$(docker image inspect "$candidate" --format '{{.Id}}')
rollback_id=$(docker image inspect ani365-bot:rollback --format '{{.Id}}')
while IFS= read -r old_id; do
  if [[ "$old_id" != "$current_id" && "$old_id" != "$rollback_id" ]]; then
    # No --force: Docker also protects images still referenced by containers/tags.
    docker image rm "$old_id" >/dev/null 2>&1 || true
  fi
done < <(docker image ls --no-trunc -q --filter 'label=org.opencontainers.image.title=ani365-bot' | sort -u)
