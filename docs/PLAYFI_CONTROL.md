# Play-Fi local protocol

Reverse-engineered from the DTS Play-Fi Android app so the integration can read
the speaker's own now-playing — AirPlay and Spotify Connect, which no other
local interface exposes — and control it, without interrupting playback. See
[PROTOCOL.md](PROTOCOL.md) for the non-Play-Fi interfaces.

## In one sentence

Play-Fi devices exchange `<api>…</api>` XML documents **encrypted with Blowfish
(ECB)** under a fixed key shared by every Play-Fi app and device; the speaker
broadcasts its state on the LAN, and accepts commands over a short TCP
connection — neither requires pairing.

## Cipher and framing

- **Cipher:** Blowfish, ECB mode, key `qAC3A4e26uDrpqUM` (16 bytes). This is not
  a per-user or per-device secret; it is a static obfuscation key embedded in
  the app, identical across installs. It lives in `const.py`.
- **Send:** take the XML, append a newline, null-pad to the 8-byte block size,
  Blowfish-encrypt, then append one `\n` delimiter. (Byte-for-byte identical to
  the app.)
- **Receive:** drop a trailing `\n` delimiter if present, Blowfish-decrypt, and
  read the `<api>…</api>` document (ignore any padding after it).

Because the on-wire bytes are ciphertext, the XML never appears in a raw packet
capture or a TLS hook — only after the app's native decrypt. The key was
recovered by disassembling `libphsslsocket.so` (standard Blowfish, confirmed by
decrypting live captures and re-encrypting a command to the exact wire bytes).

## Now playing (read) — passive, no pairing

The speaker **broadcasts** an announce to `255.255.255.255:10102` every few
seconds, unprompted. Decrypting it yields:

```
<api><phoruslink><event>
  <type>msg-phorus-broadcast-device-status</type>   (or speaker-to-phone-announce-response-pulse)
  <payload>
    <deviceid/> <ipaddress/> <name/> <capricaversion/> <volume/> <pstate/>  (1 = playing, 2 = paused)
    <metadata>
      <artist/> <album/> <trackname/> <imageurl/> <msource/>  (airplay_media, spotify_media, …)
      <station/>
      <transportcontrols> … PLAY_PAUSE / NEXT / PREVIOUS, each with <enabled> … </transportcontrols>
    </metadata>
    …
  </payload>
</event></phoruslink></api>
```

The integration binds UDP `10102`, keeps the announces whose IP matches the
speaker, and surfaces the metadata and transport state for the speaker's own
sessions: AirPlay (`airplay_media`) and Spotify Connect (`spotify_media`), both
verified, and any other service the speaker announces, handled generically
(the display name is derived from the `msource` value, e.g. `tidal_media` →
Tidal). For Spotify Connect, `<imageurl>` is the Spotify CDN cover. This is
read-only and never interrupts playback.

Now and then the speaker sends a status frame without the `<metadata>` block,
mostly around source changes. The integration keeps the last full state
through such a frame unless they continue for several seconds.

## Control — a short TCP exchange, no pairing

Open a TCP connection to the speaker on port `10113`, send one encrypted
document, read the `granted` reply, close. Two commands are implemented:

| Purpose | Document |
| --- | --- |
| Transport | `<api><query>31</query><transport>C</transport></api>` |
| Volume | `<api><query>56</query><remotevolumes><deviceids>ID:</deviceids><volumes>V:</volumes><internalvolumes>IV:</internalvolumes></remotevolumes></api>` |

Transport code `C` (from the app's enum): `0` shuffle-on, `1` shuffle-off,
`2` repeat-on, `3` repeat-off, `4` previous, `5` next, `6` play, `7` pause.
For volume, `V` is the target level or `255` to mute; `IV` is the current
internal volume.

## What is and isn't used

- The integration uses the UDP broadcast for now-playing and TCP/10113 for
  transport control of an active AirPlay or Spotify Connect session. Master
  volume is set through
  the Cast receiver (the speaker's real volume), so the Play-Fi volume command
  is implemented but not relied on.
- Cover art comes from the announce's `<imageurl>`. For AirPlay the speaker
  leaves it empty (verified with several senders, including Spotify via
  AirPlay), so no AirPlay artwork is shown. When a source does populate it, a
  relative path is resolved against the speaker's HTTP server on port `8081`
  and Home Assistant proxies the image.
- The mutual-TLS XML channel on port 10109 that an earlier draft targeted is a
  separate, legacy control path the app does not use; pairing on it interrupts
  the active source. It is intentionally avoided.

## Code

- `playfi.py` — pure cipher, framing, message builders, announce parser
  (`PlayFiState`). Unit-tested against real captured frames in
  `tests/test_playfi.py`.
- `playfi_client.py` — the UDP listener and the TCP control calls.
