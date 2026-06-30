#!/usr/bin/env python3
"""Provision GBFlash EEPROM, flash bootloader through ISP, then flash firmware through serial update."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import gbflash_ch579_isp as isp
import gbflash_provision_8byte_autowchisp as provision
import gbflash_unlock_backend as backend


class CliError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def prompt_reconnect(message: str) -> None:
    print(f"\n{message}")
    try:
        input("Press Enter after the device is back in ISP mode...")
    except EOFError:
        print("No interactive input available; continuing immediately.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision the GBFlash CH579 EEPROM credential, flash a bootloader through ISP, then flash fw.bin through serial update.",
    )
    parser.add_argument("--bootloader", type=Path, help="GBFlash bootloader image flashed through CH579 ISP")
    parser.add_argument("--firmware", type=Path, help="GBFlash serial fw.bin or zip containing fw.bin")
    parser.add_argument(
        "--unlock-key",
        default="",
        help="optional 8-byte credential as hex; if omitted it is generated from the connected UID",
    )
    parser.add_argument(
        "--usb-selector",
        default="auto",
        help="USB selector: auto or VID:PID[:bus:address], for example 4348:55e0",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="optional zero-based CH579 ISP device index after filtering VID:PID 4348:55e0/1a86:55e0",
    )
    parser.add_argument(
        "--serial-port",
        default="auto",
        help="GBFlash CH340 serial port for fw.bin update, or auto (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("provisioning-output"),
        help="directory for EEPROM backup/images (default: %(default)s)",
    )
    parser.add_argument("--wait-timeout", type=float, default=120.0, help="seconds to wait for ISP reconnect")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="seconds between ISP polls")
    parser.add_argument(
        "--flash-no-verify",
        action="store_true",
        help="accepted for compatibility; GBFlash serial update always performs its bootloader CRC finalize step",
    )
    parser.add_argument("--list-devices", action="store_true", help="list detected CH579 ISP USB devices and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_devices:
        for index, item in enumerate(isp.list_isp_devices()):
            print(f"{index}: {item['display']}")
        return 0

    if args.bootloader is None:
        raise CliError("--bootloader is required unless --list-devices is used")
    if args.firmware is None:
        raise CliError("--firmware is required unless --list-devices is used")

    bootloader = args.bootloader.expanduser().resolve()
    firmware = args.firmware.expanduser().resolve()
    if not bootloader.is_file():
        raise CliError(f"Bootloader image not found: {bootloader}")
    if not firmware.is_file():
        raise CliError(f"Firmware image not found: {firmware}")

    if args.unlock_key.strip():
        credential = backend.normalize_credential(args.unlock_key)
    else:
        uid, credential = backend.generate_unlock_key(
            usb_selector=args.usb_selector,
            device_index=args.device,
            log=log,
        )
        log(f"Generated unlock key for {provision.uid_text(uid)}: {backend.format_credential(credential)}")

    uid = backend.unlock_and_flash(
        bootloader=bootloader,
        firmware=firmware,
        credential=credential,
        output_dir=args.output_dir,
        usb_selector=args.usb_selector,
        device_index=args.device,
        serial_port=args.serial_port,
        wait_timeout=args.wait_timeout,
        poll_interval=args.poll_interval,
        flash_no_verify=args.flash_no_verify,
        log=log,
        prompt=prompt_reconnect,
    )
    log(f"Provisioning and firmware flash completed for UID {provision.uid_text(uid)}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        raise SystemExit(130)
    except (CliError, backend.UnlockError, provision.ProvisioningError, isp.Ch579IspError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
