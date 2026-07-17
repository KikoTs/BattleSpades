#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <immutable-image-tag>" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/deploy/docker-compose.example.yml"
env_file="${BATTLESPADES_ENV_FILE:-$root/deploy/.env}"
state_file="${BATTLESPADES_ROLLOUT_STATE:-$root/deploy/.last-successful-tag}"
next_tag="$1"

if [[ ! -f "$env_file" ]]; then
  echo "missing deployment environment: $env_file" >&2
  exit 2
fi
if [[ ! "$next_tag" =~ ^(sha-[0-9a-f]{7,40}|v[0-9A-Za-z._-]+)$ ]]; then
  echo "rollout requires an immutable sha-* or v* image tag" >&2
  exit 2
fi

export BATTLESPADES_IMAGE_TAG="$next_tag"
compose=(docker compose --env-file "$env_file" -f "$compose_file")
services=(eu-ctf eu-tdm eu-zombie)
ports=(27015 27025 27035)

echo "Pulling BattleSpades image tag $next_tag"
"${compose[@]}" pull

for index in "${!services[@]}"; do
  service="${services[$index]}"
  port="${ports[$index]}"
  echo "Updating $service on UDP $port"
  "${compose[@]}" up -d --no-deps --force-recreate "$service"

  healthy=false
  for _ in $(seq 1 30); do
    if python3 "$root/deploy/a2s_probe.py" --port "$port" --timeout 1.0; then
      healthy=true
      break
    fi
    sleep 2
  done
  if [[ "$healthy" != true ]]; then
    echo "$service failed its A2S health gate; stopping rollout" >&2
    "${compose[@]}" logs --tail 120 "$service" >&2 || true
    exit 1
  fi
done

printf '%s\n' "$next_tag" > "$state_file"
echo "BattleSpades fleet is healthy on $next_tag"
