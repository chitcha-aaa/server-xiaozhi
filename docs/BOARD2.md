# Board 2 (SKU `lynai`) — what the server needs

Board 2 is a different board from board 1, not another unit of the same one.

| | Board 1 | Board 2 |
|---|---|---|
| Chip | ESP32-C3, 1 core, no PSRAM | ESP32-S3, 2 core, 8 MB PSRAM |
| Flash | 8 MB | 16 MB |
| Firmware | xiaozhi 1.4.7 (Mar 2025) | xiaozhi 2.1.4 (29 May 2026) |
| SKU | `zuowei-c3-lcd` | `lynai` |
| MAC | `34:cd:b0:b4:64:b0` | `44:bd:8d:7b:39:50` |
| UUID | `aa24ffe5-ab0c-49eb-9876-d5059b9d0d76` | `d834795e-bdcd-4ece-a6de-51b5f3abad6a` |
| WS path baked in | `/ws` | `/joytalks/v1/`, but it uses whatever `/ota/` returns |
| ota_0 | `0x100000` | `0x20000` |
| Serial | native USB, COM4 | CH340K, COM5, needs `--before usb-reset` |

Verified the same on both, from board 2's boot log and a real conversation with
its stock server: protocol `version: 1`, opus 16 kHz mono, AFE 1 mic 16 kHz, and
the state sequence starting → activating → idle → connecting → listening →
speaking → listening → idle. So one WebSocket handler serves both.

## Server changes (in the working tree, not committed)

`server.py` (+61) and `config.example.yaml` (+10):

1. Nothing added for board 2's old URL path. It was tried as an alias of `/ws`
   and removed again: the board takes its WebSocket URL from the `/ota/` reply
   and connects to `/ws`, so the alias was never used.
2. `DEVICE_TOKENS`: per-board tokens, so board 2 can be revoked on its own.
   From env `DEVICE_TOKENS="name:token,..."` or `devices:` in `config.yaml`.
   `DEVICE_TOKEN` stays board 1's and is loaded as `board1`.
3. `OTA_DEVICE_TOKENS`: MAC → board name, from `device_ids:` in `config.yaml` or
   env `DEVICE_IDS`. `/ota/` now returns `websocket.token`, picking that board's
   token by its `Device-Id` header.
4. Auth compares the Bearer token against every known token in constant time and
   logs which board connected.

## Local test result (2026-11-18, `main` @ 576bde7 + working tree)

- `/ota/` with `Device-Id: 44:bd:8d:7b:39:50` returns board 2's own 32-char token.
- `mock_device.py` with that token: 1/1 turns answered, logged as
  `Authenticated device connected (board2)`.
- A wrong token is rejected with 403.

## Board 2 on the local server, end to end (2026-11-18 20:16)

Its NVS `wifi/ota_url` now points at a PC on the hotspot
(`http://192.168.38.158:8000/ota/?pad=1`, padded to the old string's 37 bytes so
the NVS entry keeps its span). `websocket/url` was left as it was.

- Board called `/ota/` on boot and took the WebSocket URL and token from the reply:
  `WS: Connecting to websocket server: ws://192.168.38.158:8000/ws with version: 1`.
- 5 spoken turns, all answered. End of speech was detected every time
  (`User finished speaking`), none hit the 10 s cap.
- STT 2.0–3.6 s, first audio 3.7–5.8 s.
- Board 2's mic is far louder than board 1's: RMS 3800–8800 against 400–1200, so
  the VAD thresholds tuned for board 1 have plenty of headroom here.
## The standby screen's clock, date and temperature

`/ota/` now returns a `server_time` section (`timestamp` in ms UTC, `timezone`,
`timezone_offset` in minutes — the field names xiaozhi 1.4.7 parsed), configurable
with `TIMEZONE` / `TIMEZONE_OFFSET_MIN`, default `Asia/Bangkok` / 420 so a server
running abroad does not hand the board its own local time. After this the board
stopped logging `No server_time section found!` and the clock reads correctly.

Whether the clock is right *because of* `server_time` is unconfirmed: the board
also gets a time from the vendor's weather service
(`WeatherApi: 完整时间: 2026-11-18T20:34:36.000ZWED`), and the screen shows
`time=00:00` until that fetch returns, which suggests it may use that one.

Two things on that screen cannot be fixed from this server:

- **Temperature** comes from `http://weather.jcwolf.space/api/weather?mac=<MAC>`,
  a vendor service whose URL is compiled into the firmware, not in NVS. It
  reported 31.5 °C and 32.0 °C, which the user says is wrong — likely located by
  IP. Changing it would mean redirecting that host by DNS and serving its format.
- **Date formatting** is the firmware's own: it renders `N O V 1 8 W E D`, and
  already splits the day as `1 8` before the screen sees it.

## What is still needed

1. **Render env**: set `DEVICE_TOKENS=board2:<token>` and
   `DEVICE_IDS=44:bd:8d:7b:39:50:board2` (or keep both in `config.yaml`).
   Board 2's token is in the local `config.yaml`, which is gitignored.
2. **Point board 2 at this server**: its NVS holds `wifi/ota_url` and
   `websocket/url` as plain strings, so no firmware patching is needed — unlike
   board 1, where the URLs were baked into the binary. Only `ota_url` should need
   changing: firmware 2.1.4 appears to save the token from `/ota/` and send it
   back as a Bearer header. That behaviour is inferred from the code comment and
   has not been confirmed on the board.
3. Decide whether board 2 gets its own LangMind agent or shares board 1's.
4. Board 2 announces 12 MCP tools the server currently ignores, among them
   `screen.snapshot`, `screen.preview_image`, `positioning` and
   `reminder.set_reminder`.

## Backups

`recovery\board2_backup_full.bin`, 16,777,216 bytes,
sha256 `73EBD6787BDD1335E4EE6F2E4C6C2C42254796DCA631C128EFADD13CC07685BA`
(plus `board2_part0..3.bin`). Restore with `write-flash 0 <file>`.

These files and `board2_nvs.bin` contain the Wi-Fi password and joytalks tokens
in clear text — do not share them.
