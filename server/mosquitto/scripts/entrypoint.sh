#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# CamSec – Mosquitto entrypoint
#
# Behaviour controlled by environment variables:
#   MQTT_AUTH   (default: 0)
#     0 → anonymous access (allow_anonymous true, no password file)
#     1 → authenticated access (allow_anonymous false, password file)
#
#   MQTT_USERS  (required when MQTT_AUTH=1)
#     Space-separated list of "username:password" pairs.
#     Example: MQTT_USERS="cam01:secret detector:pass123"
#
# The script rewrites /mosquitto/config/mosquitto.conf before launching the
# broker so that the correct authentication mode is always active.
# ─────────────────────────────────────────────────────────────────────────────
set -e

CONF=/mosquitto/config/mosquitto.conf
PASSWD=/mosquitto/config/passwd
MQTT_AUTH="${MQTT_AUTH:-0}"

if [ "$MQTT_AUTH" = "1" ]; then
    echo "[entrypoint] Authentication: ENABLED"

    if [ -z "$MQTT_USERS" ]; then
        echo "[entrypoint] ERROR: MQTT_AUTH=1 but MQTT_USERS is empty." >&2
        echo "[entrypoint]        Set MQTT_USERS='user1:pass1 user2:pass2' in your .env file." >&2
        exit 1
    fi

    # Build password file from MQTT_USERS pairs
    rm -f "$PASSWD"
    for pair in $MQTT_USERS; do
        user="${pair%%:*}"
        pass="${pair#*:}"
        if [ -z "$user" ] || [ -z "$pass" ]; then
            echo "[entrypoint] WARNING: Skipping malformed entry '$pair'" >&2
            continue
        fi
        mosquitto_passwd -b "$PASSWD" "$user" "$pass"
        echo "[entrypoint]   + user '$user' added"
    done

    # Enable authenticated mode in config
    sed -i \
        -e 's/^allow_anonymous.*/allow_anonymous false/' \
        -e 's|^#\s*password_file.*|password_file '"$PASSWD"'|' \
        -e 's|^password_file.*|password_file '"$PASSWD"'|' \
        "$CONF"

else
    echo "[entrypoint] Authentication: DISABLED (anonymous access)"

    # Disable authentication in config
    sed -i \
        -e 's/^allow_anonymous.*/allow_anonymous true/' \
        -e 's|^password_file.*|# password_file '"$PASSWD"'|' \
        "$CONF"
fi

# Hand off to the official Mosquitto entrypoint
exec /docker-entrypoint.sh "$@"
