# Play-Fi commands

Request templates and their query IDs, from `libphsslsocket.so` in the Play-Fi
Android app (8.7.1.1491). Cipher and framing are in
[PLAYFI_CONTROL.md](PLAYFI_CONTROL.md).

> Only `transport` (31) and `remotevolumes` (56) are tested. Rows marked ⚠️ can
> update firmware, change power state, turn off Cast or send credentials.

## Reading the tables

- Each request is `<api><query>N</query><ELEMENT>…</ELEMENT></api>` over
  TCP/10113.
- **ID** is the query ID `N` the app sends (see
  [Where the IDs come from](#where-the-ids-come-from)). An ID marked `?` is
  likely but not certain.
- **Reply** is the element the speaker answers with, e.g.
  `<remote_volumes_request>granted</remote_volumes_request>`. It is taken from
  the strings next to each template; for `remotevolumes` it matches a capture.
- `%s` is text and `%d` a number in the full templates (see the library).

## `<query>` requests

### Playback and volume

| ID | Element | Fields | Reply |
| --- | --- | --- | --- |
| 31 | `transport` | number: 0 shuffle on, 1 off, 2 repeat on, 3 off, 4 previous, 5 next, 6 play, 7 pause | `transport` |
| 56 | `remotevolumes` | `deviceids`, `volumes` (255 = mute), `internalvolumes`, each `X:`-separated | `remote_volumes_request` |
| 17 | `remotevolume` | `volume`, `volumeinternal` | `remote_volume` |
| 16 | `audiocue` | text, optional `channel` | `audio_cue` |

### Groups and stereo pairs

| ID | Element | Fields | Reply |
| --- | --- | --- | --- |
| 8 add, 9 remove, 10 edit, 11 rename, 12 delete | `group` | `deviceid` (before it), `name`, `oldname`, `channel`, `leader` | `group_add_device`, `group_remove_device`, `group_edit_device`, `group_rename`, `group_delete` |
| 18 add, 19 remove, 20 edit, 21 rename | `servicegroup` | `deviceid` (before it), `name`, `oldname`, `servicetype`, `leaderid`, `deviceid-001`… per member | `service_group_add`, `_remove`, `_edit`, `_rename` |
| 33 link, 32 unlink | `remotelink` / `remoteunlink` | `deviceid` + extra | `remotelink` / `remoteunlink` |
| 13 add, 14 remove, 15 rename, 40 swap | `multichannel` | `deviceid` (before it), `name`, `oldname`, `channel` | `add_multichannel`, `remove_`, `rename_`, `swap_multichannel` |
| 72 | `multichannel` | `balance` | `stereopair_attribute_balance` |
| 67 | `multichanneldelay` | `deviceid`, `channel`, `delayms` | `multichanneldelay` |
| 69 | read | multichannel channel configuration | |

### Presets and playback handover

| ID | Element | Fields | Reply |
| --- | --- | --- | --- |
| 60 | `presetmanagement` | `index`, `count` (read) | |
| 61 | `presetmanagement` | `id`, track and service fields, `primaryvolume`, `linkeddeviceids`, `devicevolumes` (store) | |
| 66 | `presetindexplay` | `presetindex` | `presetindexplay` |
| 50 | `slingplayback` | track and service fields, `primarydeviceid`, `primarydevicevolume`, `primaryvolumeoverride`, `linkeddeviceids`, `linkedvolumes`, `linkedvolumeoverrides` | |

Track and service fields: `playlisttoken`, `artist`, `album`, `trackname`,
`imageurl`, `service`, `stationid`, `serviceicon`, `extra`, `contenttype`,
`trackindex`, `trackoffset`, `shufflemode`, `repeatmode`, `username`,
`clmode`, `sqt` (`slingplayback` adds `stationname`).

### Device, sound and power

| ID | Element | Fields | Reply |
| --- | --- | --- | --- |
| 22, 25 | *(none)* | bare query; 22 stops audio redistribution | |
| 23 set, 82 get | `application` | `name`, `sessionid` | `change_name` |
| unknown | `source` | followed by `deviceid`, `devicemake`, `devicemodel`, `devicename`, `deviceappalias` (cancels a pending request) | |
| 36 | `dspdelay` | `delayms` | `dspdelay` |
| 70 set, 71 get | `set_crossover` | number | `set_crossover`, `get_crossover` |
| 24 connect, 35 disconnect | `headfi` | `deviceid`, `input`, `headphoneonly` | `headfi` |
| 37 | read/set | headphone input source (separate builder) | |
| 73 | `connectionenable` | number | `connection_enable` |
| 74 | `networkqualitysetting` | number | `networkqualitysettingresponse` |
| 68 | `devicecloudrequest` | empty | `resultmessage` |
| 28 ? | ⚠️ `powermode` | number | `powermode` |
| 57 | ⚠️ `hostpowersleepmode` | `enable` | `hostpowersleepmoderesponse` |
| 41 recovery, 42 final, 43 cancel, 44 MCU | ⚠️ `deviceupdate` | `deviceid`, `updateuri`, `updateversion`, `updatecasturi`, `updatecastversion` | |

### Rear speakers, Cast, services and Alexa

| ID | Element | Fields |
| --- | --- | --- |
| 26 distance, 27 level, 75 HT enable, 76, 77, 78 channel mask | `rcn` | one of: `deviceid` + `distance` + `channel`; `deviceid` + `level` + `channel`; `deviceid` + `htenable`; `deviceid`; `deviceid` + `channelbitmask` |
| 24 ? | ⚠️ `rcn` | `ssid`, `security`, `password`, `channel`, `timeout`, `latency` |
| 79 set, 80 get, 81 reporting | ⚠️ `cast` | `castenable`, `castdevicereportingenable` |
| 62 start, 63 pause, 64 stop | `castplaybackstart` / `castplaybackpause` / `castplaybackstop` | empty |
| 65 | `castplaybackmetadata` | `artist`, `albumartist`, `album`, `track`, `subtitle`, `composer`, `genre`, `uri`, `coverarturi`, `audioformat`, `tracknumber`, `discnumber`, `duration` |
| 49 | ⚠️ `musicservicelogin` | `service`, `username`, `password`, `profileid` |
| 29, 30, 45–48, 52–55, 59 | `alexarequest` | empty (29, 45, 46, 48), or one of: `authcode` + `clientid` + `redirecturi` (30); `locale` (47); `notification_sounds_enabled` (52); `request_start_sound_enabled` (53); `request_end_sound_enabled` (54); `alexa_3pda_master_set` (55); `alexa_microphone_enable` (59) |
| 51 logout, 58 info | `alexa3pdarequest` / `alexa3pdarequestgetinfo` | empty |

Refusals seen in the library: `denied-full`, `denied-cleanup`,
`denied-pairing-not-allowed`, `denied-windows-nopremium`,
`denied-windows-deviceupdating`.

## Where the IDs come from

The IDs are constants compiled into the native library, not values from the
Java code. Each command has a JNI wrapper, such as
`Java_com_phorus_playfi_sdk_controller_SSLSocketJNI_groupAddDevice`. The
wrapper loads its ID into a register and calls a shared builder, which
`snprintf`s the ID into the template:

```
groupAddDevice:  mov w3, 8  ->  bl fcn.0005445c  ->  snprintf("<api><query>%d</query><deviceid>%s</deviceid><group>…")
```

Builders that serve several commands `switch` on the ID minus a base, so each
case number plus the base gives the ID. The preset builder, for example, uses
base 49, and case 17 (`presetindexplay`) is ID 66. Single-command builders put
the ID right before `snprintf` (`mov w3, 0x42` for `presetindexplay`).

As a check, the transport wrapper passes `0x1f` (31), the ID the integration
already uses.

Gaps:

- `negotiateCancel` (`source`) gets its ID from its caller, which wasn't
  traced.
- Two builders both map to 24: `headfi` connect and the `rcn` Wi-Fi settings.
  No wrapper for the `rcn` one was found, so `headfi` is the likelier user.
- `powermode` shares a builder with ID 22 and is compared against 28 there,
  but no wrapper was found for it.
- IDs 0–7, 34, 38, 39 and 83 have no template in the library, so they may be
  plain reads or come from another code path.

## Legacy `<phoruslink>` events

The library also has `phone-to-speaker-*` event templates for the old
mutual-TLS channel on port 10109, which the app does not use (see
PLAYFI_CONTROL.md). Types: `announce-broadcast-pulse`, `keep-alive-pulse`,
`pairing-request`, `unpairing-request`, `track-changed`,
`report-transport-control`, `volume-change-request`, `control-track-pause`,
`track-resume`, `link-device`, `unlink-device`, `set-device-name`,
⚠️ `device-update-request`, ⚠️ `device-update-recovery-request`,
⚠️ `reboot-device-for-update-request`, `device-audio-cue-request`,
`track-data-ack`, `track-seek`, `track-metadata`,
`media-format-change-request`, `switch-audio-input`, `switch-input-request`,
`start-redist-request`, `update-metadata-request`, `multichannel-request`,
`unmultichannel-response`, `third-party-data-passthrough-request`,
`third-party-query-data-passthrough-request`, `rcn-connect-request`,
`delete-rear-channel-request`, ⚠️ `music-service-login-request`,
`3pda-certificate-signing-request`, `3pda-device-configuration-request`.
