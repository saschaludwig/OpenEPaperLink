# Wiring: Yellow AP ESP32-C6 module to NanoPi NEO

The Yellow AP board carries two chips: an ESP32-S3 (host, web UI, images)
and an ESP32-C6 (802.15.4 radio, optional CC1101 Sub-GHz).  The Linux access
point replaces only the S3.  The C6 keeps its stock OpenEPaperLink firmware
and talks to the NanoPi over the same two UARTs and two control lines the S3
used.

All signals are 3.3 V.  Never connect the C6 to the 5 V pins of the NEO.

## Signal list

| Function (S3 define) | S3 GPIO | C6 pin | NEO header pin | NEO signal | Linux name | Direction |
|---|---|---|---|---|---|---|
| `FLASHER_AP_RXD` – data from C6 | GPIO18 | GPIO3 (C6 UART TX) | 10 | UART1_RX (GPIOG7) | `/dev/ttyS1` RX, GPIO 199 | C6 → NEO |
| `FLASHER_AP_TXD` – data to C6 | GPIO17 | GPIO2 (C6 UART RX) | 8 | UART1_TX (GPIOG6) | `/dev/ttyS1` TX, GPIO 198 | NEO → C6 |
| `FLASHER_AP_RESET` – C6 reset | GPIO47 | EN / CHIP_PU | 7 | GPIOG11 | GPIO 203 | NEO → C6, idle high, low pulse resets |
| `FLASHER_DEBUG_PROG` – C6 boot select | GPIO21 | GPIO9 (BOOT) | 12 | GPIOA6 | GPIO 6 | NEO → C6, idle high, low = download mode |
| `FLASHER_DEBUG_TXD` – C6 console out | GPIO19 | GPIO16 (U0TXD) | 22 | UART2_RX (GPIOA1) | `/dev/ttyS2` RX, GPIO 1 | C6 → NEO |
| `FLASHER_DEBUG_RXD` – C6 console in | GPIO20 | GPIO17 (U0RXD) | 11 | UART2_TX (GPIOA0) | `/dev/ttyS2` TX, GPIO 0 | NEO → C6 |
| `FLASHER_RGB_LED` – WS2812 data in | GPIO48 | – (LED on the C6 board) | 19 | SPI0_MOSI (GPIOC0) | `/dev/spidev0.0` | NEO → LED, optional |
| 3.3 V supply | – | 3V3 | 1 | SYS_3.3V | – | power |
| Ground | – | GND | 6 | GND | – | power |

Notes

* UART1 (`/dev/ttyS1`) carries the access point protocol.  The service opens
  it at 115200 baud and switches the C6 to 2 000 000 baud with the `HSPD`
  command, exactly as the S3 does.  The H3 UART supports this rate.
* UART2 (`/dev/ttyS2`) is only used to flash the C6 through its ROM
  bootloader (`esptool`).  During normal operation the C6 prints its console
  there; the service ignores it.
* Reset and boot are driven through `libgpiod` (`/dev/gpiochip0`).  Both
  lines are active low on the C6 and must idle high.  If the Yellow AP board
  has pull-ups on EN and GPIO9 they stay in place.
* The NEO's own UART0 (header pins 3/4 of the 4-pin debug header) stays the
  Linux serial console and is not used here.
* The CC1101 Sub-GHz radio of the Yellow AP stays connected to the C6; no
  additional wiring is needed.
* The RGB status LED (WS2812B) sits on the C6 board, but its data input is
  routed through the board-to-board connector to S3 GPIO48; the C6 firmware
  does not drive it.  Find that contact on the connector and wire it to NEO
  pin 19 (SPI0 MOSI).  The service sends the WS2812 bit stream through SPI
  (3 SPI bits per LED bit at 2.4 MHz).  The LED runs from 3.3 V on the
  Yellow AP, so the 3.3 V MOSI level is fine.  If the LED flickers, add a
  330 Ω resistor in series with the data line.  Leave pin 19 unconnected and
  set `OEPL_RGB_LED_SPI_DEVICE=` (empty) to run without the LED.
* Keep the wires short (< 20 cm).  At 2 Mbaud longer leads cause framing
  errors; if that happens, set `OEPL_SERIAL_AP_HIGH_SPEED=0` in the systemd
  unit to stay at 115200 baud.

## Header orientation

NanoPi NEO 24-pin header, viewed from the top with the Ethernet jack facing
away from you.  Odd pins are on the inner row.

```
 1  SYS_3.3V   2  VDD_5V
 3  I2C0_SDA   4  VDD_5V
 5  I2C0_SCL   6  GND
 7  GPIOG11    8  UART1_TX   (GPIOG6)
 9  GND       10  UART1_RX   (GPIOG7)
11  UART2_TX  12  GPIOA6
13  UART2_RTS 14  GND
15  UART2_CTS 16  UART1_RTS
17  SYS_3.3V  18  UART1_CTS
19  SPI0_MOSI 20  GND
21  SPI0_MISO 22  UART2_RX   (GPIOA1)
23  SPI0_CLK  24  SPI0_CS
```

Used pins: 1, 6, 7, 8, 10, 11, 12, 22 and 19 for the RGB LED.  Enabling the
SPI overlay also claims pins 21, 23 and 24 (MISO, CLK, CS); leave them free.

## Enabling the UARTs on the NEO

DietPi: `dietpi-config` → Advanced Options → Serial/UART → enable `ttyS1`
and `ttyS2`.  Armbian/Ubuntu: `armbian-config` → System → Hardware → enable
`uart1` and `uart2`, or add `overlays=uart1 uart2` to `/boot/armbianEnv.txt`.
Reboot and check that `/dev/ttyS1` and `/dev/ttyS2` exist.

Make sure no getty is attached to these ports:

```
sudo systemctl disable --now serial-getty@ttyS1.service serial-getty@ttyS2.service
```

## Enabling SPI for the RGB LED

DietPi: `dietpi-config` → Advanced Options → SPI state → On.  Armbian/Ubuntu:
`armbian-config` → System → Hardware → enable `spi-spidev`, or add
`overlays=spi-spidev` and `param_spidev_spi_bus=0` to `/boot/armbianEnv.txt`.
Reboot and check that `/dev/spidev0.0` exists.  The service needs write
access to it (it runs as root with the default unit).
