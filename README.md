# Loewe klang (DTS Play-Fi) for Home Assistant

A custom Home Assistant integration for the **Loewe klang** series of DTS
Play-Fi speakers. It shows what is currently playing and lets you control the
speaker from Home Assistant, much like the Play-Fi app.

This integration works with **any source** whereas the existing integrations (Cast, DLNA) only support their own protocol.

So far it has only been tested on the **klang mr3**. Other klang models use the
same Play-Fi platform and should work, but are unverified. Reports are welcome.

## Features

### Now playing and control

The speaker can be fed by several sources. The integration follows whichever
one is active and shows it on a single media player:

| | Google Cast | UPnP / DLNA | AirPlay | Spotify Connect | Other Play-Fi services⁴ |
| --- | :---: | :---: | :---: | :---: | :---: |
| Title and artist | ✓ | ✓ | ✓ | ✓ | ✓ |
| Album | ✓ | ✓ | ✓¹ | ✓ | ✓ |
| Cover art | ✓ | ✓ | – | ✓ | ✓ |
| Progress and duration | ✓ | ✓ | – | ✓³ | – |
| Play and pause | ✓ | ✓ | ✓ | ✓ | ✓⁵ |
| Next and previous | ✓² | ✓ | ✓ | ✓ | ✓⁵ |
| Seek | ✓² | ✓ | – | ✓³ | – |
| Stop | ✓ | ✓ | – | pauses | – |
| Shuffle and repeat | – | ✓ | – | – | – |
| Source shown as | `Chromecast: <app>` | `DLNA`, … | `AirPlay` | `Spotify Connect` | Service name, e.g. `Tidal` |
| Data comes from | Cast media status | UPnP renderer | The speaker's Play-Fi broadcast | Home Assistant's Spotify integration, or the speaker's Play-Fi broadcast | The speaker's Play-Fi broadcast |

¹ Shown when the AirPlay sender provides it.

² When the Cast app allows it.

³ Only with Home Assistant's Spotify integration set up. The Play-Fi broadcast
has no position.

⁴ Services started from the Play-Fi app, such as Tidal, Qobuz, Deezer or
internet radio. They are handled generically from the same broadcast and have
not been tested, because they need subscriptions. Radio streams that report
only a station show the station as the title.

⁵ Only the controls the speaker marks as available for that service.

Volume and mute work the same for every source: they use the Cast receiver's
master volume, with UPnP as fallback. See [AirPlay](#airplay) and
[Spotify Connect](#spotify-connect) for how those two work and what they need.

### Other features

| Feature | How it works |
| --- | --- |
| Play media, TTS and the media browser | Cast default media receiver, with DLNA push as fallback |
| Auto discovery | SSDP. The speaker announces itself as a Phorus MediaRenderer |
| Firmware version sensors | Play-Fi firmware and (optional) Chromecast firmware, read from the speaker |
| Firmware update available | Chromecast built-in update flag from the device's setup info |
| Diagnostics download | Raw UPnP, Cast and Play-Fi state, for troubleshooting |

Tested on a Loewe klang mr3 with Home Assistant 2026.2.3 and 2026.10.4.

## Installation

### HACS

1. In HACS, open **Custom repositories**.
2. Add this repository's URL with the category **Integration**.
3. Install **Loewe klang (DTS Play-Fi)** and restart Home Assistant.

### Manual

Copy `custom_components/loewe_klang` into your Home Assistant
`config/custom_components/` folder and restart Home Assistant.

## Setup

The speaker is normally discovered automatically. Confirm it under
**Settings → Devices & services**. You can also add it manually by choosing
**Add integration → Loewe klang** and entering the speaker's IP address.

Give the speaker a fixed IP address in your router if you can. If its address
changes, SSDP rediscovery updates the entry automatically once the old address
stops answering as this speaker.

The speaker also shows up in Home Assistant's built-in **Google Cast** and
**DLNA Digital Media Renderer** integrations. You can ignore those discoveries,
or keep the Cast entity alongside this one.

## Entity

The integration creates one `media_player` entity, for example
`media_player.loewe_klang_mr3` (the ID follows your speaker's name). Besides the standard media player attributes it
exposes the following:

- `source`: the active input, such as `Chromecast: Spotify`, `DLNA` or `AirPlay`.
- `play_fi_medium`: the raw PlayMedium value from the renderer.
- `airplay_session`: true while an AirPlay sender is connected, even when paused.
- `playfi_source`: the source the speaker reports in its Play-Fi broadcast, such as `airplay_media`.
- `spotify_entity`: the Spotify entity being mirrored during Spotify Connect playback.
- `upnp_transport_state`, `cast_app_id` and `cast_connected`: these help with troubleshooting.

It also creates firmware diagnostic entities:

- `sensor.loewe_klang_mr3_play_fi_firmware`: the installed Play-Fi firmware revision.
- `sensor.loewe_klang_mr3_chromecast_firmware`: the Chromecast build (disabled by default).
- `binary_sensor.loewe_klang_mr3_firmware_update_available`: on when the Chromecast
  built-in reports a firmware update. The Chromecast part updates itself over the
  air; the **Play-Fi firmware is still updated through the Play-Fi app or a USB
  stick** — see [docs/PROTOCOL.md](docs/PROTOCOL.md) for why there is no in-app
  update button.

Example: play a file or stream.

```yaml
action: media_player.play_media
target:
  entity_id: media_player.loewe_klang_mr3
data:
  media_content_type: music
  media_content_id: https://example.com/stream.mp3
```

## AirPlay

When you AirPlay to the speaker from an iPhone or Mac, neither UPnP nor Cast
sees the session. The speaker does, though: it relays the sender's track
metadata and accepts transport commands over the local Play-Fi protocol, the
same way the Play-Fi app shows and controls AirPlay from another device.

The integration listens to the speaker's Play-Fi status broadcast and shows the
title, artist, album and play/pause state with source `AirPlay`. Play, pause,
next and previous are sent back to the speaker, which forwards them to the
AirPlay sender. Volume uses the Cast master volume as usual. Reading the broadcast is
passive, so it never interrupts what is playing.

State changes appear within a couple of seconds, at the speaker's broadcast
rate. The album appears when the sender provides it, but the speaker does not
relay AirPlay cover art, so none is shown. There is no progress bar or seeking
for AirPlay either, because the speaker does not report the position.

Network requirements:

- Home Assistant must receive **UDP broadcasts on port 10102** from the
  speaker. This works out of the box on Home Assistant OS and in containers
  with host networking. In a bridged Docker or VM network the broadcast does
  not arrive.
- Home Assistant must reach the speaker on **TCP port 10113** for control.

If the broadcast is unavailable, an AirPlay session still shows as state `on`
with source `AirPlay` and volume control only. The protocol details are in
[docs/PLAYFI_CONTROL.md](docs/PLAYFI_CONTROL.md).

## Spotify Connect

When you play from the Spotify app with the speaker as the target, the speaker
streams from Spotify by itself, so neither UPnP nor Cast sees the session. The
integration gets it from one of two places.

**Without any setup**, it reads the speaker's Play-Fi broadcast, the same one
used for [AirPlay](#airplay). That gives the track, artist, album, cover art
and play/pause state with source `Spotify Connect`. Play, pause, next and
previous are sent to the speaker. Updates arrive within a couple of seconds.
The broadcast carries no playback position, so there is no progress bar or
seeking. The same network requirements as AirPlay apply.

**With Home Assistant's built-in Spotify integration** set up, that takes
priority and adds progress, duration and seeking. Commands then go through
Spotify. Any Spotify account in Home Assistant that is playing on this speaker
is picked up automatically. To limit it to one account, open **Settings →
Devices & services → Loewe klang → Configure** and pick that account's Spotify
entity. Updates arrive at the Spotify integration's polling rate, so a track
change can take up to about 30 seconds to show.

Cast and UPnP sessions on the speaker take priority over both.

## Limitations

- **Other Play-Fi services.** Streams started from the Play-Fi app (Tidal,
  Qobuz, radio and so on) are shown from the speaker's broadcast using a
  generic rule, but none has been tested yet, because they need
  subscriptions.
  If one shows nothing, download the diagnostics while it plays (the `playfi`
  block shows what the speaker reports) and open an issue.
- **Grouping and zones**, **input switching**, **presets** and **EQ** are
  Play-Fi app features with no local API, so they are not supported.
- **UPnP volume** on this firmware is a per-session DLNA value that the speaker
  may ignore. Volume and mute therefore go through Cast whenever it is
  connected.

## Development

```bash
uv venv -p 3.14 .venv
VIRTUAL_ENV=.venv uv pip install homeassistant pytest-homeassistant-custom-component pychromecast
.venv/bin/python -m pytest
```

The tests use responses recorded from a real klang mr3, stored under
`tests/fixtures`, and Play-Fi frames captured off the wire (`tests/test_playfi.py`).
See [docs/PROTOCOL.md](docs/PROTOCOL.md) for what was learned about the speaker's
network interfaces and [docs/PLAYFI_CONTROL.md](docs/PLAYFI_CONTROL.md) for the
Play-Fi protocol.
