# GBFlash Unlocker

Qt6 GUI and CLI workflow for GBFlash provisioning:

- provision the CH579 EEPROM credential through native WCH USB ISP
- flash a selected bootloader image through CH579 ISP
- flash a selected `fw.bin`/firmware package through the GBFlash serial updater

Main entry points:

- `gbflash_unlock_app.py`: Qt6 GUI
- `gbflash_provision_then_flash.py`: CLI workflow
- `gbflash_ch579_isp.py`: native CH579 ISP transport wrapper
- `gbflash_serial_update.py`: GBFlash serial firmware updater
- `gbflash_provision_8byte_autowchisp.py`: credential/provisioning algorithm reference
- `tests/`: unit tests for provisioning and unlocker backend helpers

Packaging:

- macOS: `./build_gbflash_unlock_macos.sh`
- Linux: `./build_gbflash_unlock_linux.sh`
- Windows: `.\build_gbflash_unlock_windows.ps1`

Install dependencies into a venv with:

```bash
python3 -m pip install -r requirements-gbflash-unlock.txt
```

PyInstaller builds are native-target builds: build Windows on Windows, Linux on
Linux, and macOS on macOS. A Linux build can also be produced from macOS using a
Linux container with this directory mounted as the working directory.

Run tests:

```bash
python3 -m unittest discover -s tests
```

## Unlock Sequence

The unlock credential is stored in the CH579 EEPROM/DataFlash. It is an 8-byte
RSA-signature-style record derived from the CH579 UID. This is not encryption in
the usual confidentiality sense; it is a compact authenticity check.

For an 8-byte UID, only 6 bytes are signed:

```text
UID[0], UID[1], UID[2], UID[3], UID[4], UID[0]
```

Those 6 bytes are split into two 3-byte big-endian integers:

```python
m0 = int_be(message[0:3])
m1 = int_be(message[3:6])
```

Each block is signed with the private exponent:

```python
s = pow(m, D, N)
```

Each signature is stored as a 4-byte little-endian word:

```text
EEPROM[0x0000:0x0004] = s0 little-endian
EEPROM[0x0004:0x0008] = s1 little-endian
```

Validation reverses this with the public exponent:

```python
m = pow(signature, E, N)
```

and checks that the decoded 3-byte blocks match `uid[:5] + uid[:1]`.

### Key Size

The RSA modulus is:

```text
n = 0x8E9A928D = 2392494733
```

That is only 32 bits. Each signed block is a 32-bit integer, but the actual
message per block is only 24 bits. This is not cryptographically secure RSA; it
is only a compact firmware-side authenticity mechanism.

### Credential And EEPROM Addresses

```text
CH579 EEPROM/DataFlash size      0x0800 bytes / 2 KiB
Credential EEPROM offset         0x0000
Credential EEPROM size           0x0008 bytes
Application EEPROM mirror base   0x3E800  (0x7D << 11)
```

The provisioning workflow reads the full 2 KiB EEPROM image, patches only bytes
`0x0000..0x0007`, then writes the full 2 KiB image back. Writing the full image
preserves the rest of the EEPROM contents while updating the credential.

### Unlock Check

The firmware checks the EEPROM credential against the CH579 UID. If it matches,
the application sets registration state bytes in RAM:

```text
0x20000118  credential / legacy acceptance state
0x20000119  secondary registration status byte
```

The unregistered predicate returns `1` when the device is considered
unregistered and `0` when it is registered. This predicate is used both for the
serial status response and for deciding whether the serial firmware update
handoff is allowed.

Important firmware addresses in the unpatched direct application image:

```text
0x4410  read/copy CH579 UID
0xA844  credential check wrapper
0xA8EC  RSA public decode wrapper
0xA914  read credential bytes from EEPROM/DataFlash mirror
0xA940  refresh registration state
0xA96C  unregistered predicate
0xA518  serial command 0xF1 handler / update handoff gate
0x5C80  write bootloader magic and reset
```

## Memory Map

```text
0x0000..0x3DFF  bootloader code region
0x3E00..0x3E0D  boot-info record used by the bootloader
0x4000..        application firmware image
0x20000090      retained RAM bootloader request word
0x20000118      registration state byte
0x20000119      secondary registration state byte
```

The bootloader request word uses:

```text
0x20000090 = 0xAA55BB01
```

When the application writes this value and resets the MCU, the original
bootloader enters serial update mode instead of jumping to the application.

## Bootloader Region (`0x0000..0x4000`)

There are three bootloader variants relevant to this work.

### Minimal Clone Bootloader

Initializes basic hardware, then immediately jumps to application firmware at
`0x4000`. This variant usually lacks the serial update implementation.

### Public/Hacked Bootloader

This bootloader can enter update mode, but on the tested clone hardware it
appears to depend on clocking assumptions that only work when the board has the
additional crystal populated at the `X1` pad.

### Original Bootloader

The original bootloader was recovered by dumping the bootloader region from a
working image. It contains the serial update mode, activity LED blinking while
waiting for update packets, application validation, and the flash-writing
routines.

## Boot-Info Record

The bootloader validates the application before jumping to it. The boot-info
record lives at `0x3E00` and is 14 bytes:

```text
offset  size  meaning
0x00    2     marker 0x5555
0x02    4     ASCII "LFBG"
0x06    2     CRC16 of application bytes at 0x4000
0x08    4     application length
0x0C    2     CRC16 of record bytes 0x00..0x0B
```

The application jump also checks that the first application vector at `0x4000`
looks like an SRAM stack pointer:

```text
(*(uint32_t *)0x4000 & 0x2FFE0000) == 0x20000000
```

If boot-info validation or the vector-table check fails, the bootloader remains
in update mode.

## Update Behavior

The bootloader updates the application firmware at `0x4000` through the CH340
serial interface at `2,000,000 baud`.

Update mode can be entered in three ways:

- U22 is held/pressed during boot, causing the bootloader to stay in update mode.
- The application receives serial command `0xF1`, confirms the device is
  registered, receives confirmation byte `0x01`, writes `0xAA55BB01` to
  `0x20000090`, and resets.
- The bootloader decides the application is invalid, for example because the
  boot-info record or vector table check fails.

While in update mode, the original bootloader flashes the activity LED
continuously and waits for update packets.

### Application Update Trigger

The app-side serial update trigger is:

```text
host -> app: 0xF1
app  -> host: one-byte response/status
host -> app: 0x01
app action: write 0xAA55BB01 to 0x20000090 and reset
```

The `0xF1` handler is registration-gated. If the device is unregistered, the
handler returns an error/status path and does not write the bootloader magic.

## Serial Firmware Package

The serial updater accepts either:

- a raw `fw.bin`
- a ZIP archive containing `fw.bin`

The package payload is the application firmware data that the bootloader writes
to application space at `0x4000`. The bootloader also writes/regenerates the
boot-info record at `0x3E00` when finalizing the update.

### Serial Packet Structure

All bootloader update packets use this framing:

```text
offset  size  endian  meaning
0x00    4     BE      intro marker 0x48484A4A
0x04    1     -       sender, host uses 0
0x05    2     BE      sequence number
0x07    2     BE      command
0x09    2     BE      payload length
0x0B    N     -       payload
...     4     BE      outro marker 0x4A4A4848
```

If the total packet length is odd, one zero padding byte is appended.

### Serial Update Commands

```text
command  meaning
0x21     initialize update session
0x24     write firmware data packet
0x23     finalize update / write boot-info
```

The updater sends command `0x21` twice, matching FlashGBX behavior. The response
contains the bootloader program size and page size.

Command `0x24` payload:

```text
offset  size  endian  meaning
0x00    2     BE      packet index, starting at 1
0x02    2     BE      chunk length
0x04    N     -       firmware chunk bytes
...     2     BE      CRC16 of this chunk
```

Command `0x23` payload:

```text
offset  size  endian  meaning
0x00    2     BE      CRC16 of complete fw.bin
0x02    2     BE      bitwise inverse of that CRC16
```

The CRC is the bootloader/FlashGBX nibble-table CRC16 initialized with `0xFFFF`.

## Issues With Clones

Clone devices usually do not contain the official firmware and bootloader:

- Public firmware images are available, but clone firmware often patches out the
  unlock check instead of provisioning a valid credential.
- The original bootloader is not publicly distributed, so many clones use a
  trimmed-down bootloader that only boots the application and lacks serial update
  mode.

This leads to practical issues:

- A clone may report registered/unlocked while still showing clone behavior such
  as slow response, slow read speed, or slow write speed.
- The device may not be updateable through FlashGBX because the minimal
  bootloader lacks the serial update implementation.
