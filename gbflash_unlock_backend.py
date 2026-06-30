#!/usr/bin/env python3
"""Backend operations for the GBFlash Unlock GUI."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

import gbflash_ch579_isp as isp
import gbflash_provision_8byte_autowchisp as provision
import gbflash_serial_update as serial_update


LogFn = Callable[[str], None]
PromptFn = Callable[[str], None]


class UnlockError(RuntimeError):
    """Expected GUI-facing error."""


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


def _prompt(prompt: PromptFn | None, log: LogFn | None, message: str) -> None:
    _log(log, message)
    if prompt is not None:
        prompt(message)


def _sleep_before_prompt() -> None:
    time.sleep(1.0)


def _isp_needs_user_reconnect(error: Exception) -> bool:
    text = str(error).lower()
    return (
        "no ch579 wch isp usb device found" in text
        or "native usb device not found" in text
        or "0x00" in text
        or "/0x00" in text
        or "unexpected chip_id/type" in text
    )


def _ensure_isp_available(
    *,
    usb_selector: str,
    device_index: int | None,
    wait_timeout: float,
    poll_interval: float,
    log: LogFn | None,
    prompt: PromptFn | None,
    action: str,
) -> None:
    _sleep_before_prompt()
    try:
        isp.read_uid(usb_selector=usb_selector, device_index=device_index, log=log)
        return
    except Exception as exc:
        if not _isp_needs_user_reconnect(exc):
            raise
        _prompt(
            prompt,
            log,
            f"Reconnect the device in CH579 ISP mode, then continue to {action}.",
        )
        isp.wait_for_ch579(
            usb_selector=usb_selector,
            device_index=device_index,
            timeout_seconds=wait_timeout,
            poll_seconds=poll_interval,
            log=log,
        )


def _run_isp_transaction(
    *,
    action: str,
    usb_selector: str,
    device_index: int | None,
    wait_timeout: float,
    poll_interval: float,
    log: LogFn | None,
    prompt: PromptFn | None,
    operation,
):
    _ensure_isp_available(
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        action=action,
    )
    try:
        return operation()
    except Exception as exc:
        if not _isp_needs_user_reconnect(exc):
            raise
        _sleep_before_prompt()
        try:
            return operation()
        except Exception as retry_exc:
            if not _isp_needs_user_reconnect(retry_exc):
                raise
        _prompt(
            prompt,
            log,
            f"Reconnect the device in CH579 ISP mode, then continue to {action}.",
        )
        isp.wait_for_ch579(
            usb_selector=usb_selector,
            device_index=device_index,
            timeout_seconds=wait_timeout,
            poll_seconds=poll_interval,
            log=log,
        )
        return operation()


def _wait_for_serial_port(
    *,
    serial_port: str,
    timeout: float,
    poll_interval: float,
    prompt: PromptFn | None,
    log: LogFn | None,
) -> str:
    requested = (serial_port or "auto").strip()
    started = time.monotonic()
    prompted = False
    if requested.lower() == "auto":
        _sleep_before_prompt()
    while True:
        if requested.lower() != "auto":
            return requested
        ports = serial_update.autodetect_ports()
        if ports:
            return ports[0]
        if not prompted:
            _sleep_before_prompt()
            ports = serial_update.autodetect_ports()
            if ports:
                return ports[0]
            _prompt(
                prompt,
                log,
                "Reconnect the device in normal GBFlash serial mode, then continue to flash firmware.",
            )
            prompted = True
        if timeout > 0 and time.monotonic() - started >= timeout:
            raise UnlockError(f"No CH340 GBFlash serial port found within {timeout:g} seconds")
        time.sleep(poll_interval)


def normalize_credential(text: str) -> bytes:
    """Parse an 8-byte credential from flexible hex text."""
    text = re.sub(r"0x", "", text, flags=re.IGNORECASE)
    compact = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(compact) != provision.CREDENTIAL_SIZE * 2:
        raise UnlockError("Unlock key must contain exactly 8 bytes / 16 hex characters")
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise UnlockError("Unlock key contains invalid hex") from exc


def format_credential(credential: bytes) -> str:
    if len(credential) != provision.CREDENTIAL_SIZE:
        raise UnlockError(f"Unlock key must be {provision.CREDENTIAL_SIZE} bytes")
    return credential.hex(" ").upper()


def generate_unlock_key(
    *,
    usb_selector: str = "",
    device_index: int | None = None,
    log: LogFn | None = None,
) -> tuple[bytes, bytes]:
    """Read the connected UID and return (uid, credential)."""
    _log(log, "Reading CH579 UID...")
    uid = isp.read_uid(usb_selector=usb_selector, device_index=device_index, log=log)
    credential = provision.create_credential(uid)
    provision.validate_credential(uid, credential)
    _log(log, f"UID: {provision.uid_text(uid)}")
    _log(log, f"Generated unlock key: {format_credential(credential)}")
    return uid, credential


def provision_with_credential(
    *,
    credential: bytes,
    output_dir: Path,
    usb_selector: str = "",
    device_index: int | None = None,
    wait_timeout: float = 120.0,
    poll_interval: float = 1.0,
    log: LogFn | None = None,
    prompt: PromptFn | None = None,
) -> tuple[bytes, Path, Path]:
    """Write the supplied credential to EEPROM and verify readback."""
    if len(credential) != provision.CREDENTIAL_SIZE:
        raise UnlockError(f"Unlock key must be {provision.CREDENTIAL_SIZE} bytes")
    if wait_timeout < 0:
        raise UnlockError("wait timeout cannot be negative")
    if poll_interval <= 0:
        raise UnlockError("poll interval must be greater than zero")

    _log(log, "Reading connected CH579 UID...")
    uid = _run_isp_transaction(
        action="read UID",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.read_uid(usb_selector=usb_selector, device_index=device_index, log=log),
    )
    provision.validate_credential(uid, credential)
    _log(log, f"UID: {provision.uid_text(uid)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _log(log, f"Provisioning output directory: {output_dir}")
    prefix = f"uid-{provision.uid_slug(uid)}"
    image_path = output_dir / f"{prefix}-gui-provisioned-eeprom.bin"
    readback_path = output_dir / f"{prefix}-gui-readback-eeprom.bin"

    _log(log, "Reading EEPROM for credential merge...")
    current = _run_isp_transaction(
        action="read EEPROM",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.dump_eeprom(usb_selector=usb_selector, device_index=device_index, log=log),
    )
    if len(current) != provision.EEPROM_SIZE:
        raise UnlockError(f"EEPROM dump has wrong size: {len(current)} bytes")

    image = bytearray(current)
    image[
        provision.CREDENTIAL_OFFSET : provision.CREDENTIAL_OFFSET + provision.CREDENTIAL_SIZE
    ] = credential
    image_bytes = bytes(image)
    image_path.write_bytes(image_bytes)
    _log(log, f"Prepared EEPROM image: {image_path}")

    _log(log, "Writing EEPROM image...")
    _run_isp_transaction(
        action="write EEPROM",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.write_eeprom(image_bytes, usb_selector=usb_selector, device_index=device_index, log=log),
    )

    _log(log, "Waiting for device to be available in ISP mode for verification...")
    returned_uid = _run_isp_transaction(
        action="verify EEPROM",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.read_uid(usb_selector=usb_selector, device_index=device_index, log=log),
    )
    if returned_uid != uid:
        raise UnlockError(
            "A different CH579 appeared after EEPROM programming: "
            f"expected {provision.uid_text(uid)}, got {provision.uid_text(returned_uid)}"
        )

    _log(log, "Reading back EEPROM...")
    readback = _run_isp_transaction(
        action="read back EEPROM",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.dump_eeprom(usb_selector=usb_selector, device_index=device_index, log=log),
    )
    if len(readback) != provision.EEPROM_SIZE:
        raise UnlockError(f"EEPROM readback has wrong size: {len(readback)} bytes")
    readback_path.write_bytes(readback)
    if readback != image_bytes:
        raise UnlockError(
            "EEPROM verification failed: "
            f"{provision.first_difference(image_bytes, readback)}"
        )

    _log(log, "EEPROM verification successful.")
    return uid, image_path, readback_path


def flash_firmware(
    *,
    firmware: Path,
    serial_port: str = "auto",
    timeout: float = 180.0,
    no_erase: bool = False,
    no_verify: bool = False,
    no_reset: bool = False,
    log: LogFn | None = None,
    prompt: PromptFn | None = None,
    poll_interval: float = 1.0,
) -> None:
    firmware = firmware.expanduser().resolve()
    if not firmware.is_file():
        raise UnlockError(f"Firmware image not found: {firmware}")
    if no_erase:
        raise UnlockError("GBFlash serial firmware flashing does not support no-erase mode")
    if no_verify:
        _log(log, "GBFlash serial updater does not expose a separate verification toggle.")
    if no_reset:
        _log(log, "Skipping serial bootloader trigger is not exposed in the GUI workflow.")

    _log(log, f"Flashing firmware through GBFlash serial updater: {firmware}")
    try:
        port = _wait_for_serial_port(
            serial_port=serial_port,
            timeout=timeout,
            poll_interval=poll_interval,
            prompt=prompt,
            log=log,
        )
        fw_data = serial_update.load_firmware_package(firmware)
        _log(log, f"Using serial port: {port}")
        _log(log, f"Firmware size: {len(fw_data)} bytes, crc16=0x{serial_update.crc16(fw_data):04X}")
        serial_update.update_port(
            port,
            fw_data,
            timeout=0.5,
            skip_trigger=False,
            log=log,
        )
    except Exception as exc:
        raise UnlockError(f"Firmware flash failed:\n{exc}") from exc
    _log(log, "Firmware flash completed.")


def flash_bootloader(
    *,
    bootloader: Path,
    usb_selector: str = "",
    device_index: int | None = None,
    wait_timeout: float = 120.0,
    poll_interval: float = 1.0,
    log: LogFn | None = None,
    prompt: PromptFn | None = None,
) -> None:
    bootloader = bootloader.expanduser().resolve()
    if not bootloader.is_file():
        raise UnlockError(f"Bootloader image not found: {bootloader}")
    if wait_timeout < 0:
        raise UnlockError("wait timeout cannot be negative")
    if poll_interval <= 0:
        raise UnlockError("poll interval must be greater than zero")

    _log(log, f"Flashing bootloader through CH579 ISP: {bootloader}")
    _run_isp_transaction(
        action="flash bootloader",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.flash_firmware(
            bootloader,
            usb_selector=usb_selector,
            device_index=device_index,
            verify=True,
            log=log,
        ),
    )
    _log(log, "Bootloader flash completed.")
    _run_isp_transaction(
        action="reset out of CH579 ISP",
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
        operation=lambda: isp.reset_from_isp(
            usb_selector=usb_selector,
            device_index=device_index,
            log=log,
        ),
    )
    _log(log, "CH579 ISP reset command sent.")


def unlock_and_flash(
    *,
    bootloader: Path,
    firmware: Path,
    credential: bytes,
    output_dir: Path = Path("provisioning-output"),
    usb_selector: str = "",
    device_index: int | None = None,
    serial_port: str = "auto",
    wait_timeout: float = 120.0,
    poll_interval: float = 1.0,
    flash_timeout: float = 180.0,
    flash_no_verify: bool = False,
    log: LogFn | None = None,
    prompt: PromptFn | None = None,
) -> bytes:
    uid, _image, _readback = provision_with_credential(
        credential=credential,
        output_dir=output_dir,
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
    )
    flash_bootloader(
        bootloader=bootloader,
        usb_selector=usb_selector,
        device_index=device_index,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        log=log,
        prompt=prompt,
    )
    flash_firmware(
        firmware=firmware,
        serial_port=serial_port,
        timeout=flash_timeout,
        no_verify=flash_no_verify,
        log=log,
        prompt=prompt,
        poll_interval=poll_interval,
    )
    return uid
