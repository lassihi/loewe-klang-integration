# Loewe klang mr3 network interfaces

These notes were collected on 2026-10-29 by probing a klang mr3. The speaker
reports AirPlay model `MR3_GC`, firmware `50871009`, built Feb 25 2026, and
Cast build `1.56.310669`.

## Summary

| Port | Protocol | Use in this integration |
| --- | --- | --- |
| 49152/tcp | UPnP MediaRenderer from Phorus, libupnp 1.6.19 | Transport state, metadata, transport control, DLNA push |
| 8008/tcp, 8443/tcp | Cast HTTP setup API, `/setup/eureka_info` | Cast UUID and MAC address |
| 8009/tcp | Google Cast v2 | Now playing for Cast sessions, master volume and mute, play media |
| 7000/tcp | AirPlay 2, `/info` returns a binary plist | Model and firmware |
| 10102/udp | Play-Fi state broadcast, about every 2 s | Now playing for the speaker's own sessions (AirPlay). Blowfish-encrypted XML, decrypted by the integration — see [PLAYFI_CONTROL.md](PLAYFI_CONTROL.md) |
| 10113/tcp | Play-Fi control | Transport (play/pause/next/previous) and volume for the active session. Blowfish-encrypted XML |
| 10001, 101xx, 21002, 21100, 5002, 9000, 9013/tcp | Proprietary Play-Fi | Other binary channels, not used |

No LinkPlay-style `httpapi.asp` endpoint exists, even though the renderer uses
LinkPlay-like PlayMedium names.

## UPnP renderer quirks

- The device description is at `http://<ip>:49152/description.xml`. It
  reports manufacturer `Phorus`, model `Phorus-Renderer`, and the friendly name
  `Loewe klang mr3`. The SSDP matcher keys on `manufacturer: Phorus`.
- SOAP responses use a **non-standard namespace**, for example
  `urn:upnp-org:serviceId:AVTransport` instead of the service type. Strict
  UPnP clients may reject them, which is likely why Home Assistant's DLNA DMR
  integration does not work well with Play-Fi speakers. The client in
  `upnp.py` matches elements by local name only.
- The server **closes the TCP connection after every reply**. Reusing pooled
  HTTP connections fails with "Server disconnected". The client sends
  `Connection: close` and retries once.
- `GetDeviceCapabilities` lists these PlayMedia values:
  `NONE, STATION-NETWORK, SONGLIST-NETWORK, SONGLIST-LOCAL, SONGLIST-LOCAL_TF,
  THIRD-DLNA, AIRPLAY, QPLAY, SPOTIFY, UNKNOWN`. `GetMediaInfo` reports the
  current one in `PlayMedium`.
- The renderer **keeps the last item's metadata after Stop**. The integration
  hides metadata unless the state is PLAYING, PAUSED or TRANSITIONING.
- `ListPresets` and `GetVolumeDBRange` return UPnP error 501.
- **Volume:** `GetVolume` and `SetVolume` act on a DLNA session value. The
  speaker was observed reporting 30 and later 9 while the real master volume
  stayed at 30%, and it ignored `SetVolume` while idle. GENA events are
  unreliable too. The first NOTIFY carried a stale volume as unescaped XML, and
  a second NOTIFY carried the correct value, escaped. For these reasons the
  integration polls instead of subscribing, and uses Cast for volume.
- An extra `urn:schemas-tencent-com:service:QPlay:2` service exists for
  QQ Music queues. It is not used.

## Cast

- The Cast UUID equals `ssdp_udn` from `/setup/eureka_info`.
- `volume_control_type` is `master`, so Cast volume is the speaker's real
  volume. Setting 0.35 resulted in 0.37, which suggests the speaker maps values
  to its own volume scale, most likely 30 levels 1/30 apart. The volume buttons
  step by one level, because a 0.02 step could not get below values such as 0.17.
- Changing the volume clears mute.
- Launching the Default Media Receiver (`CC1AD845`) took about 3 to 6 seconds
  in testing. Metadata, pause, resume, seek and stop all worked.

## Firmware

- `/setup/eureka_info` (port 8008) reports `build_version`, `cast_build_revision`
  and `has_update` for the Chromecast built-in. The Chromecast part updates
  itself over the air; `has_update` drives the update-available binary sensor.
- The AirPlay `/info` plist (port 7000) reports `firmwareRevision`, the installed
  Play-Fi firmware, surfaced as the Play-Fi firmware sensor.

### Why there is no in-app Play-Fi update button

The DTS Play-Fi Android app was examined (androguard) to see whether Home
Assistant could trigger Play-Fi firmware updates the way the app does. It can't,
practically:

- **The app pushes updates to the speaker.** It copies firmware zips to local
  storage, starts an HTTP server on the phone serving
  `http://<phone-ip>:8080/update/system5.zip` and `/playcast.zip`
  (`com.phorus.playfi.sdk.update.UpdateServerService`), then sends an update
  command to the speaker, which pulls and flashes the zip. Progress is polled.
- **The control channel is custom native TLS** (`SSLSocketJNI` →
  `libphsslsocket.so` / `libphcrypto.so`), carrying encrypted XML with a
  hardcoded auth token. Replaying the update command would require
  reimplementing that native crypto.
- **The firmware image is not in the APK.** `assets/` ships only MCU zips for
  other brands; the mr3 `system5.zip` / `playcast.zip` arrive in a separately
  downloaded Google Play OBB expansion file.
- A malformed update command can brick the speaker.

Given all of the above, firmware updates stay with the Play-Fi app or a USB
stick; the integration only reports versions and the update-available flag.

## Verified end to end

The speaker was at zero volume and muted throughout this test. The original
volume was restored afterwards.

- Cast `play_media` updated the entity with state, title, duration, source and
  app name. Pause, resume, seek and stop all worked.
- DLNA push with `SetAVTransportURI` and `Play` updated the entity with state,
  title and source `DLNA`. Pause and resume worked.

## Spotify Connect

Verified live while Spotify Connect was playing on the speaker:

- UPnP `GetTransportInfo` reports `STOPPED`, with `PlayMedium` `THIRD-DLNA`
  and no track metadata.
- The Cast receiver has no app running and no media status.
- The AirPlay `/info` plist has no now-playing data.
- The speaker advertises `_spotify-connect._tcp` on port 81. Its ZeroConf
  endpoint `/goform/spotifyConnect?action=getInfo` returns only device identity
  (name, device ID, library version), never the current track.

The track exists only in Spotify's cloud. The integration therefore mirrors the
Home Assistant Spotify entity whose `source` is this speaker. The Play-Fi app
does the same: its Spotify screen uses Spotify's own Connect flow.

## Play-Fi control channel

The speaker's own now-playing and transport control travel over Play-Fi as
Blowfish-encrypted `<api>…</api>` XML: a state broadcast on UDP 10102 and a
short TCP exchange on 10113. This is what the integration uses for AirPlay
now-playing and control. The full protocol, cipher and message formats are in
[PLAYFI_CONTROL.md](PLAYFI_CONTROL.md).

(An earlier draft targeted a separate mutual-TLS XML channel on port 10109 that
the app does not use and whose pairing interrupts the active source; it was
abandoned in favour of the broadcast above.)

## AirPlay

Verified live during AirPlay playback:

- UPnP reports `STOPPED` with no metadata, and Cast is idle.
- The AirPlay `/info` plist `statusFlags` (and the `flags` / `sf` TXT records of
  `_airplay._tcp` and `_raop._tcp`) change from `0x404` to `0xc04`. Bit `0x800`
  is set while a sender holds a session. It stays set while the sender is
  paused and clears when the sender disconnects.
- The sender pushes track metadata and a remote-control handle to the speaker,
  which relays both over its Play-Fi state broadcast (see
  [PLAYFI_CONTROL.md](PLAYFI_CONTROL.md)) — not over any of the AirPlay or UPnP
  interfaces above.

The integration reads that broadcast to show the AirPlay title, artist and
play/pause state and to offer play/pause/next/previous, and uses the Cast
receiver volume for volume. If the broadcast is unavailable it falls back to a
plain `on` / `AirPlay` indicator based on the `statusFlags` bit above.

## Not yet verified

- What the UPnP renderer reports while the **Play-Fi app** streams a service
  or radio station.
