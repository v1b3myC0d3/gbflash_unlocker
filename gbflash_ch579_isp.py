#!/usr/bin/env python3
"""CH579 WCH USB ISP helpers for GBFlash provisioning."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from chisp_flasher.backends.wch_legacy_usb import Backend, USB_TIMEOUTS_MS
from chisp_flasher.core.errors import BackendError
from chisp_flasher.protocol.commands import (
    CMD_DATA_ERASE,
    CMD_DATA_PROGRAM,
    CMD_DATA_READ,
    CMD_ISP_END,
    CMD_PROGRAM,
    CMD_READ_CFG,
)
from chisp_flasher.protocol.crypto import xor_crypt
from chisp_flasher.protocol.native_usb import (
    build_data_erase,
    build_data_program,
    build_data_read,
    build_isp_end,
    build_program,
    build_read_cfg,
)
from chisp_flasher.transport.usb_native import UsbNativeDeviceInfo, UsbNativeLink


CHIP = "CH579"
EEPROM_SIZE = 2 * 1024
EEPROM_SECTOR_SIZE = 1024
PROGRAM_CHUNK_SIZE = 56
DATA_READ_CHUNK_SIZE = 0x3A
CH579_ISP_VIDS = {0x4348, 0x1A86}
CH579_ISP_PID = 0x55E0

LogFn = Callable[[str], None]


@dataclass(slots=True)
class ConfigState:
    data0: str = "0x00"
    data1: str = "0x00"
    wrp0: str = "0xFF"
    wrp1: str = "0xFF"
    wrp2: str = "0xFF"
    wrp3: str = "0xFF"
    no_key_serial_download: bool | None = None
    download_cfg: bool | None = None
    cfg_reset_en: bool | None = None
    cfg_debug_en: bool | None = None
    cfg_boot_en: bool | None = None
    cfg_rom_read: bool | None = None


class Ch579IspError(RuntimeError):
    """Expected CH579 ISP transport error."""


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


def _candidate_infos() -> list[UsbNativeDeviceInfo]:
    candidates = [
        info
        for info in UsbNativeLink.list_candidate_infos()
        if info.vid in CH579_ISP_VIDS and info.pid == CH579_ISP_PID
    ]
    return sorted(
        candidates,
        key=lambda item: (
            0 if item.vid == 0x4348 else 1,
            item.bus or -1,
            item.address or -1,
            item.interface_number or -1,
        ),
    )


def list_isp_devices() -> list[dict]:
    return [
        {
            "selector": info.selector,
            "display": info.display_text,
            "vid": info.vid,
            "pid": info.pid,
            "bus": info.bus,
            "address": info.address,
            "interface_number": info.interface_number,
            "endpoint_out": info.endpoint_out,
            "endpoint_in": info.endpoint_in,
        }
        for info in _candidate_infos()
    ]


def resolve_selector(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
) -> str:
    selector = str(usb_selector or "").strip()
    if selector and selector.lower() != "auto":
        return selector

    candidates = _candidate_infos()
    if not candidates:
        raise Ch579IspError("No CH579 WCH ISP USB device found (expected VID:PID 4348:55e0 or 1a86:55e0)")

    if device_index is None:
        return candidates[0].selector

    if device_index < 0 or device_index >= len(candidates):
        raise Ch579IspError(
            f"USB device index {device_index} is out of range; found {len(candidates)} CH579 ISP device(s)"
        )
    return candidates[device_index].selector


class Ch579Isp:
    def __init__(
        self,
        *,
        usb_selector: str = "",
        device_index: int | None = None,
        trace: bool = False,
        log: LogFn | None = None,
    ) -> None:
        self.selector = resolve_selector(usb_selector=usb_selector, device_index=device_index)
        self.trace = bool(trace)
        self.log = log
        self.backend = Backend()

    def _open_link(self) -> UsbNativeLink:
        link = self.backend._make_link(self.selector, trace=self.trace)
        link.open()
        return link

    def _identify_and_read_cfg(self, link: UsbNativeLink):
        chip_cfg = self.backend._chip_cfg(CHIP)
        candidates = self.backend._normalize_identify_candidates(chip_cfg)
        parsed, _matched = self.backend._probe_identify_native(link, candidates)
        chip_id = parsed.data[0]
        chip_type = parsed.data[1]
        _log(self.log, f"Identify OK: chip_id=0x{chip_id:02X} chip_type=0x{chip_type:02X}")
        code, cfg_raw = link.txrx(build_read_cfg(), CMD_READ_CFG, USB_TIMEOUTS_MS["read_cfg"])
        if code != 0x00:
            raise Ch579IspError("read_config failed")
        cfg = self.backend._parse_cfg_response(CHIP, cfg_raw)
        _log(self.log, f"UID: {cfg.uid.hex('-').upper()}")
        return chip_id, chip_type, cfg

    def read_uid(self) -> bytes:
        with_link = self._open_link()
        try:
            with_link.flush()
            _chip_id, _chip_type, cfg = self._identify_and_read_cfg(with_link)
            return bytes(cfg.uid)
        finally:
            with_link.close()

    def read_config(self) -> dict:
        return self.backend.read_config_native_usb(CHIP, usb_selector=self.selector, log_cb=lambda _level, msg: _log(self.log, msg))

    def write_config_fields(self, **fields) -> dict:
        current = self.read_config()
        config = ConfigState(
            data0=current.get("data0_hex", "0x00"),
            data1=current.get("data1_hex", "0x00"),
            wrp0=current.get("wrp0_hex", "0xFF"),
            wrp1=current.get("wrp1_hex", "0xFF"),
            wrp2=current.get("wrp2_hex", "0xFF"),
            wrp3=current.get("wrp3_hex", "0xFF"),
            **fields,
        )
        return self.backend.write_config_native_usb(
            CHIP,
            usb_selector=self.selector,
            config=config,
            log_cb=lambda _level, msg: _log(self.log, msg),
        )

    def reset_from_isp(self) -> None:
        link = self._open_link()
        try:
            link.flush()
            self._identify_and_read_cfg(link)
            _log(self.log, "Sending ISP_END reason=1 to reset out of WCH ISP mode")
            code, _payload = link.txrx(build_isp_end(1), CMD_ISP_END, USB_TIMEOUTS_MS["isp_end"])
            if code != 0x00:
                raise Ch579IspError("isp_end reset failed")
        finally:
            link.close()

    def dump_eeprom(self) -> bytes:
        data = bytearray()
        link = self._open_link()
        try:
            link.flush()
            self._identify_and_read_cfg(link)
            address = 0
            while address < EEPROM_SIZE:
                length = min(DATA_READ_CHUNK_SIZE, EEPROM_SIZE - address)
                code, payload = link.txrx(build_data_read(address, length), CMD_DATA_READ, USB_TIMEOUTS_MS["verify"])
                if code != 0x00:
                    raise Ch579IspError(f"data_read failed at EEPROM offset 0x{address:04X}")
                chunk = payload[2:]
                if len(chunk) != length:
                    raise Ch579IspError(
                        f"data_read length mismatch at EEPROM offset 0x{address:04X}: expected {length}, got {len(chunk)}"
                    )
                data.extend(chunk)
                address += length
            return bytes(data)
        finally:
            link.close()

    def write_eeprom(self, image: bytes, *, erase: bool = True, retries: int = 3) -> None:
        if len(image) != EEPROM_SIZE:
            raise Ch579IspError(f"EEPROM image must be {EEPROM_SIZE} bytes, got {len(image)}")

        last_error: Exception | None = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                self._write_eeprom_once(image, erase=erase)
                return
            except BackendError as exc:
                last_error = exc
                _log(self.log, f"EEPROM write identify/program attempt {attempt} failed: {exc}")
                if attempt < retries:
                    _log(self.log, "Waiting for CH579 ISP to settle before retrying EEPROM write...")
                    time.sleep(1.0)
                    self.selector = resolve_selector(usb_selector=self.selector)
                    continue
                raise Ch579IspError(str(exc)) from exc
            except Ch579IspError as exc:
                last_error = exc
                if attempt < retries and "identify" in str(exc).lower():
                    _log(self.log, f"EEPROM write attempt {attempt} failed: {exc}")
                    time.sleep(1.0)
                    self.selector = resolve_selector(usb_selector=self.selector)
                    continue
                raise
        if last_error is not None:
            raise Ch579IspError(str(last_error)) from last_error

    def _write_eeprom_once(self, image: bytes, *, erase: bool) -> None:
        link = self._open_link()
        try:
            link.flush()
            chip_id, _chip_type, cfg = self._identify_and_read_cfg(link)
            xor_key = self.backend._unlock(link, cfg, chip_id, log_cb=lambda _level, msg: _log(self.log, msg))

            if erase:
                sectors = max(1, EEPROM_SIZE // EEPROM_SECTOR_SIZE)
                _log(self.log, f"Erasing EEPROM data flash: {sectors} sector(s)")
                code, _payload = link.txrx(build_data_erase(sectors), CMD_DATA_ERASE, USB_TIMEOUTS_MS["erase"])
                if code != 0x00:
                    raise Ch579IspError("data_erase failed")

            for address in range(0, EEPROM_SIZE, PROGRAM_CHUNK_SIZE):
                plain = image[address : address + PROGRAM_CHUNK_SIZE]
                enc = xor_crypt(plain, xor_key)
                code, _payload = link.txrx(
                    build_data_program(address, 0x00, enc),
                    CMD_DATA_PROGRAM,
                    USB_TIMEOUTS_MS["program"],
                )
                if code != 0x00:
                    raise Ch579IspError(f"data_program failed at EEPROM offset 0x{address:04X}")

            flush_addr = ((EEPROM_SIZE + PROGRAM_CHUNK_SIZE - 1) // PROGRAM_CHUNK_SIZE) * PROGRAM_CHUNK_SIZE
            try:
                enc_empty = xor_crypt(b"", xor_key)
                code, _payload = link.txrx(
                    build_data_program(flush_addr, 0x00, enc_empty),
                    CMD_DATA_PROGRAM,
                    USB_TIMEOUTS_MS["program"],
                )
                if code != 0x00:
                    raise Ch579IspError("empty data_program flush failed")
            except Exception:
                # wchisp uses an empty code-program packet as the final flush; keep that fallback for bootloaders
                # that ignore an empty data-program packet.
                code, _payload = link.txrx(build_program(flush_addr, 0x00, b""), CMD_PROGRAM, USB_TIMEOUTS_MS["program"])
                if code != 0x00:
                    raise Ch579IspError("empty program flush failed")

            try:
                link.txrx(build_isp_end(0), CMD_ISP_END, USB_TIMEOUTS_MS["isp_end"])
            except Exception:
                pass
        finally:
            link.close()


def read_uid(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> bytes:
    return Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).read_uid()


def dump_eeprom(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> bytes:
    return Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).dump_eeprom()


def read_config(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> dict:
    return Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).read_config()


def write_config_fields(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
    **fields,
) -> dict:
    return Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).write_config_fields(**fields)


def reset_from_isp(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> None:
    Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).reset_from_isp()


def write_eeprom(
    image: bytes,
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> None:
    Ch579Isp(usb_selector=usb_selector, device_index=device_index, log=log).write_eeprom(image)


def wait_for_ch579(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 1.0,
    log: LogFn | None = None,
) -> str:
    started = time.monotonic()
    while True:
        try:
            selector = resolve_selector(usb_selector=usb_selector, device_index=device_index)
            Ch579Isp(usb_selector=selector, log=log).read_uid()
            return selector
        except Exception as exc:
            if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
                raise Ch579IspError(f"No CH579 appeared in ISP mode within {timeout_seconds:g} seconds") from exc
            time.sleep(poll_seconds)


def flash_firmware(
    firmware: Path,
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    verify: bool = True,
    log: LogFn | None = None,
) -> dict:
    selector = resolve_selector(usb_selector=usb_selector, device_index=device_index)
    _log(log, f"Flashing {firmware} through CH579 ISP on {selector}")
    try:
        return Backend().flash_native_usb(
            CHIP,
            str(firmware),
            usb_selector=selector,
            verify=verify,
            log_cb=lambda _level, msg: _log(log, msg),
        )
    except BackendError as exc:
        raise Ch579IspError(str(exc)) from exc
