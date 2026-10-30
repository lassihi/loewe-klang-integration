"""Constants for the Loewe klang integration."""

from datetime import timedelta

DOMAIN = "loewe_klang"
CONF_UDN = "udn"
CONF_CAST_UUID = "cast_uuid"
CONF_SPOTIFY_ENTITY = "spotify_entity"
SCAN_INTERVAL = timedelta(seconds=5)

# DTS Play-Fi local protocol. The Blowfish key is a fixed obfuscation key shared
# by every Play-Fi app and device (it is not a per-user or per-device secret);
# it is what lets the speaker's own now-playing be read and controlled on the
# LAN without pairing. See docs/PLAYFI_CONTROL.md.
PLAYFI_BLOWFISH_KEY = b"qAC3A4e26uDrpqUM"
PLAYFI_BROADCAST_PORT = 10102
PLAYFI_CONTROL_PORT = 10113
# The speaker serves AirPlay cover art over plain HTTP on this port.
PLAYFI_IMAGE_PORT = 8081
