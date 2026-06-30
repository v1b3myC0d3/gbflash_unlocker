#!/usr/bin/env python3
"""Flash a GBFlash serial firmware package through the bootloader updater."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable


BAUDRATE = 2_000_000
CH340_VID = 0x1A86
CH340_PID = 0x7523
INTRO = 0x48484A4A
OUTRO = 0x4A4A4848


class UpdateError(RuntimeError):
    pass


LogFn = Callable[[str], None]


def _log(log: LogFn | None, message: str, *, end: str = "\n") -> None:
    if log is not None:
        log(message.rstrip())
    else:
        print(message, end=end)


def import_serial():
    try:
        import serial
        import serial.tools.list_ports
    except ModuleNotFoundError as exc:
        raise SystemExit("Missing dependency: install pyserial for this Python environment.") from exc
    return serial


def crc16(data: bytes) -> int:
    table = [
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
    ]
    value = 0xFFFF
    for byte in data:
        value = table[(byte ^ value) & 0x0F] ^ (value >> 4)
        value = table[((byte >> 4) ^ value) & 0x0F] ^ (value >> 4)
        value &= 0xFFFF
    return value


def pack_packet(seq_no: int, command: int, payload: bytes = b"") -> bytes:
    data = struct.pack(">IBHHH", INTRO, 0, seq_no, command, len(payload))
    data += payload
    data += struct.pack(">I", OUTRO)
    if len(data) % 2:
        data += b"\x00"
    return data


def read_exact(dev, length: int) -> bytes:
    data = dev.read(length)
    if len(data) != length:
        raise UpdateError(f"short packet read: got {len(data)} bytes, expected {length}")
    return data


def get_packet(dev) -> dict[str, int | bytes]:
    deadline = time.monotonic() + 0.5
    while dev.in_waiting == 0:
        if time.monotonic() >= deadline:
            raise UpdateError("no response from bootloader")
        time.sleep(0.001)

    header = read_exact(dev, 11)
    intro, sender, seq_no, command, payload_len = struct.unpack(">IBHHH", header)
    payload = read_exact(dev, payload_len)
    outro = struct.unpack(">I", read_exact(dev, 4))[0]
    if intro != INTRO:
        raise UpdateError(f"bad packet intro 0x{intro:08X}")
    if outro != OUTRO:
        raise UpdateError(f"bad packet outro 0x{outro:08X}")
    return {
        "sender": sender,
        "seq_no": seq_no,
        "command": command,
        "payload_len": payload_len,
        "payload": payload,
    }


def load_firmware_package(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.read("fw.bin")
    return path.read_bytes()


def autodetect_ports() -> list[str]:
    serial = import_serial()
    return [
        port.device
        for port in serial.tools.list_ports.comports()
        if port.vid == CH340_VID and port.pid == CH340_PID
    ]


def trigger_bootloader(port: str, timeout: float, skip_trigger: bool) -> None:
    if skip_trigger:
        return
    serial = import_serial()
    with serial.Serial(port, BAUDRATE, timeout=timeout) as dev:
        dev.write(b"\xF1")
        dev.read(1)
        dev.write(b"\x01")
        dev.flush()
    time.sleep(3)


def connect_bootloader(port: str, timeout: float):
    serial = import_serial()
    dev = serial.Serial(port, BAUDRATE, timeout=timeout)
    init = pack_packet(seq_no=1, command=0x21)

    # FlashGBX sends the init packet once, drains any stale bytes, then sends it
    # again. Keep that behavior because this bootloader can emit debug chatter.
    dev.write(init)
    dev.flush()
    time.sleep(0.1)
    if dev.in_waiting:
        dev.read(dev.in_waiting)
    dev.write(init)
    dev.flush()

    response = get_packet(dev)
    if response["seq_no"] != 1 or response["command"] != 0x21:
        dev.close()
        raise UpdateError(f"unexpected init response: {response}")
    payload = response["payload"]
    if len(payload) < 9 or struct.unpack(">H", payload[1:3])[0] != 0x0003:
        dev.close()
        raise UpdateError(f"bootloader rejected init: {payload.hex(' ')}")
    return dev, payload


def write_firmware(dev, fw_data: bytes, page_size: int, log: LogFn | None = None) -> None:
    total_packets = math.ceil(len(fw_data) / page_size)
    seq_no = 2
    for packet_index in range(1, total_packets + 1):
        start = (packet_index - 1) * page_size
        chunk = fw_data[start : start + page_size]
        payload = struct.pack(">HH", packet_index, len(chunk))
        payload += chunk
        payload += struct.pack(">H", crc16(chunk))

        dev.write(pack_packet(seq_no=seq_no, command=0x24, payload=payload))
        dev.flush()
        response = get_packet(dev)
        if response["seq_no"] != seq_no or response["command"] != 0x24:
            raise UpdateError(f"unexpected write response at packet {packet_index}: {response}")
        ack = response["payload"]
        if len(ack) < 3:
            raise UpdateError(f"short write ack at packet {packet_index}: {ack.hex(' ')}")
        if struct.unpack(">H", ack[:2])[0] != packet_index or ack[2] != 0x01:
            raise UpdateError(f"write failed at packet {packet_index}: {ack.hex(' ')}")

        _log(log, f"Writing: {packet_index}/{total_packets} ({packet_index / total_packets * 100:5.1f}%)", end="\r")
        seq_no += 1
    _log(log, "")

    fw_crc = crc16(fw_data)
    payload = struct.pack(">HH", fw_crc, (~fw_crc) & 0xFFFF)
    dev.write(pack_packet(seq_no=seq_no, command=0x23, payload=payload))
    dev.flush()
    response = get_packet(dev)
    ack = response["payload"]
    if response["seq_no"] != seq_no or response["command"] != 0x23:
        raise UpdateError(f"unexpected finalize response: {response}")
    if len(ack) < 1 or ack[0] != 0x01:
        raise UpdateError(f"finalize failed: {ack.hex(' ')}")


def update_port(
    port: str,
    fw_data: bytes,
    timeout: float,
    skip_trigger: bool,
    log: LogFn | None = None,
) -> None:
    _log(log, f"Using port: {port}")
    trigger_bootloader(port, timeout, skip_trigger)
    dev, payload = connect_bootloader(port, timeout)
    try:
        program_size = struct.unpack(">H", payload[3:5])[0]
        page_size = struct.unpack(">H", payload[7:9])[0]
        _log(log, f"Bootloader ready: program_size=0x{program_size:X}, page_size=0x{page_size:X}")
        write_firmware(dev, fw_data, page_size, log=log)
    finally:
        dev.close()
    time.sleep(0.8)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="raw fw.bin package or fw_GBFlash.zip")
    parser.add_argument("--port", help="serial port; auto-detects CH340 if omitted")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument(
        "--skip-trigger",
        action="store_true",
        help="device is already in serial bootloader mode; do not send app command F1/01",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fw_data = load_firmware_package(args.firmware)
    print(f"Firmware: {args.firmware} ({len(fw_data)} bytes, crc16=0x{crc16(fw_data):04X})")

    if args.port:
        update_port(args.port, fw_data, args.timeout, args.skip_trigger)
    else:
        ports = autodetect_ports()
        if not ports:
            raise SystemExit("No CH340 GBFlash serial port found; pass --port explicitly.")
        last_error: Exception | None = None
        for port in ports:
            try:
                update_port(port, fw_data, args.timeout, args.skip_trigger)
                break
            except Exception as exc:
                last_error = exc
                print(f"{port}: {exc}", file=sys.stderr)
        else:
            raise SystemExit(f"Could not update any detected port. Last error: {last_error}")

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        if exc.__class__.__name__ == "SerialException":
            print(f"Serial error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        raise
