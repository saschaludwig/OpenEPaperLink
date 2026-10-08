# OpenEPaperLink access point for Linux boards (NanoPi NEO)

`linux_ap` is a Python re-implementation of the ESP32-S3 host firmware found
in `ESP32_AP-Flasher`.  It runs on a NanoPi NEO (Allwinner H3, 512 MB RAM,
Ethernet) or any other Linux board with two free UARTs and two GPIOs and
drives the unchanged ESP32-C6 radio module of the Yellow AP.

What stays the same:

* the web interface from `ESP32_AP-Flasher/wwwroot` is served unmodified,
* the HTTP API (`/get_db`, `/save_cfg`, `/imgupload`, `/jsonupload`, ...) and
  the WebSocket messages,
* the on-disk formats (`tagDB.json`, `apconfig.json`, `tagtypes/*.json`),
* the UART protocol to the C6 and the UDP synchronisation with other APs,
* all content types from `content_cards.json`.

What differs: Wi-Fi, display, LEDs and OTA of the host are handled by Linux.
The setup page keeps working; its WLAN fields are stubs reporting the
Ethernet address.

See [docs/wiring.md](docs/wiring.md) for the pin mapping.

## Installation (DietPi or Ubuntu/Armbian)

```sh
sudo apt install python3 python3-venv python3-dev git gcc make libgpiod2 python3-libgpiod \
    python3-pil libffi-dev zlib1g-dev libjpeg-dev libfreetype-dev
sudo git clone https://github.com/OpenEPaperLink/OpenEPaperLink /opt/OpenEPaperLink
cd /opt/OpenEPaperLink/linux_ap
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
# optional but recommended: native Group5 encoder and dithering (pure Python
# fallbacks exist, the C versions are ~50x faster on the NEO):
make -C native
sudo mkdir -p /var/lib/oepl
sudo cp systemd/oepl-linux-ap.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oepl-linux-ap
```

The web interface is then available at `http://<ip>/`.

Settings for the machine (UART devices, GPIO numbers, data directory) are
environment variables in the systemd unit (`OEPL_*`) or a JSON file passed
with `--config`.  Everything the user changes in the web UI is stored in
`/var/lib/oepl/current/apconfig.json`.

### DietPi notes

* Enable `ttyS1` and `ttyS2` in `dietpi-config` → Advanced Options →
  Serial/UART and disable the login shells on them.
* DietPi ships without `python3-venv`; the `apt install` line above adds it.
* The NEO is 32-bit ARM (`armv7l`), for which PyPI has no prebuilt wheels
  of Pillow, cffi and bitarray.  `python3-pil` supplies Pillow through
  `--system-site-packages`; cffi and bitarray are compiled by pip, which
  needs `python3-dev` and `libffi-dev`.  Without `python3-pil` the Pillow
  build takes several minutes and needs the zlib/jpeg/freetype headers.
* Time comes from `systemd-timesyncd`; the AP needs a correct clock for the
  content types.  `/set_time` keeps working for isolated networks (requires
  the service to run as root or with `CAP_SYS_TIME`).
* The service wants port 80.  Run as root (default unit) or add
  `AmbientCapabilities=CAP_NET_BIND_SERVICE` and a dedicated user.

### C6 firmware update

"Update C6" in the web UI works like on the ESP: the AP service is paused,
the C6 is put into download mode via the EN/BOOT GPIOs and `esptool`
(installed into the venv) writes the files listed in `firmware_C6.json`
over the debug UART (`OEPL_SERIAL_DEBUG_PORT`, default `/dev/ttyS2`).
With an empty URL the files from `OEPL_C6_BINARIES_DIR` or the uploaded
staging directory `<data>/c6_staging` are flashed.

### RGB status LED

The WS2812 LED of the Yellow AP is driven through SPI0 MOSI (header pin 19,
`/dev/spidev0.0`, see `docs/wiring.md`).  It shows the same colours and
animations as the ESP firmware: a breathing colour per AP state (green when
online, dark blue while tags are timing out, red when failed), blink patterns
on radio errors and a green/red fade after a C6 update.  The "RGB LED
brightness" setting of the web UI applies.  Set `OEPL_RGB_LED_SPI_DEVICE=`
(empty) to disable the LED when it is not wired.

### Multiple access points

UDP synchronisation with other OpenEPaperLink APs (multicast 239.10.0.1:16033,
or broadcast when "discovery" is enabled in the AP settings) is active by
default and is wire-compatible with the ESP firmware, so a NanoPi AP and
Yellow APs can share one tag population.

## Running from a checkout

```sh
cd linux_ap
python3 -m venv .venv && .venv/bin/pip install -e .[test]
OEPL_DATA_DIR=/tmp/oepl OEPL_HTTP_PORT=8080 .venv/bin/python -m oepl_linux_ap
```

Without a C6 attached the AP state shows "offline"; the web UI, tag
database and image generation still work, which is handy for development.

## Layout

```
oepl_linux_ap/
  __main__.py      service entry point
  config.py        machine settings (OEPL_* environment)
  protocol.py      struct layouts and constants shared with C6 and tags
  tagdb.py         tag records, apconfig.json, tag types, variables
  web.py           aiohttp routes and WebSocket hub
  serialap.py      UART protocol to the C6
  newproto.py      pending queue, block transfer, data requests
  image/           dithering, bit planes, zlib, Group5
  content/         content types (contentmanager.cpp)
  flasher.py       C6 firmware update via esptool
  leds.py          WS2812 status LED over SPI
  udp.py           AP-to-AP synchronisation
native/            optional Group5 encoder shared library (ctypes)
systemd/           service unit
docs/wiring.md     pin mapping Yellow AP C6 <-> NanoPi NEO
```
