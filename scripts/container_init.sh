#!/bin/sh
set -eu

# Railway and several other volume providers mount the volume root as UID 0.
# Prepare only the dedicated data subtree as root, then permanently shed
# privileges before Python imports or any game-controlled data is processed.
data_dir="${BATTLESPADES_DATA_DIR:-/data}"
case "$data_dir" in
    /data | /data/*) ;;
    *)
        echo "BattleSpades container configuration error: BATTLESPADES_DATA_DIR must be /data or beneath it" >&2
        exit 2
        ;;
esac

umask 027

if [ "$(id -u)" -eq 0 ]; then
    install -d -m 0750 -o battlespades -g battlespades "$data_dir"
    chown battlespades:battlespades "$data_dir"
    exec gosu battlespades:battlespades \
        python -u scripts/container_entrypoint.py "$@"
fi

# Retain support for operators that explicitly launch the image with --user.
exec python -u scripts/container_entrypoint.py "$@"
