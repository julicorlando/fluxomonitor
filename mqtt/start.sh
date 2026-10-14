#!/bin/sh
set -eu
: "${MQTT_USERNAME:?Configure MQTT_USERNAME}"
: "${MQTT_PASSWORD:?Configure MQTT_PASSWORD}"
case "$MQTT_USERNAME" in
  *[!a-zA-Z0-9_-]*|'') echo 'MQTT_USERNAME: use apenas letras, numeros, _ e -' >&2; exit 1 ;;
esac
umask 077
mosquitto_passwd -b -c /mosquitto/config/passwords "$MQTT_USERNAME" "$MQTT_PASSWORD"
printf 'user %s\ntopic readwrite fluxomonitor/#\n' "$MQTT_USERNAME" > /mosquitto/config/acl
chown mosquitto:mosquitto /mosquitto/config/passwords /mosquitto/config/acl /mosquitto/data
chmod 600 /mosquitto/config/passwords /mosquitto/config/acl
unset MQTT_PASSWORD
exec mosquitto -c /mosquitto/config/mosquitto.conf
