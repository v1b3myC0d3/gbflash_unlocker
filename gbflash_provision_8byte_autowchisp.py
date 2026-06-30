#!/usr/bin/env python3
"""Provision and verify a CH579 GBFlash registration record using wchisp.

Workflow:
  1. Query CH579 information and extract its 8-byte UID.
  2. Dump and preserve the complete 2 KiB DataFlash/EEPROM.
  3. Generate the UID-specific 8-byte registration record.
  4. Patch bytes 0x0000..0x0007 of the preserved EEPROM image.
  5. Flash the complete 2 KiB image.
  6. Ask the operator to reconnect the same device in ISP mode.
  7. Wait using `wchisp probe`, dump the EEPROM, and compare it byte-for-byte.

Requires Python 3.10+. If `wchisp` is not found in PATH, the script downloads
an official prebuilt nightly release for supported platforms.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

# Supplier-provided RSA parameters used by the application verifier.
N = 0x8E9A928D
E = 0x20001
D = 0x1126CD91
P = 0x63F5
Q = 0x16D39

EEPROM_SIZE = 2 * 1024
CREDENTIAL_OFFSET = 0
CREDENTIAL_SIZE = 8
UID_SIZE = 8

WCHISP_RELEASE_TAG = "nightly"
WCHISP_RELEASE_BASE_URL = (
    f"https://github.com/ch32-rs/wchisp/releases/download/{WCHISP_RELEASE_TAG}"
)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class ProvisioningError(RuntimeError):
    """An expected provisioning error with a concise operator-facing message."""


def normalized_machine() -> str:
    machine = platform.machine().strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(machine, machine)


def wchisp_platform_asset() -> tuple[str, str, str]:
    """Return (platform key, release asset name, executable filename)."""
    system = platform.system().strip().lower()
    machine = normalized_machine()

    if system == "windows" and machine == "x86_64":
        return "win-x64", "wchisp-win-x64.zip", "wchisp.exe"
    if system == "linux" and machine == "x86_64":
        return "linux-x64", "wchisp-linux-x64.tar.gz", "wchisp"
    if system == "linux" and machine == "aarch64":
        return "linux-aarch64", "wchisp-linux-aarch64.tar.gz", "wchisp"
    if system == "darwin" and machine == "aarch64":
        return "macos-arm64", "wchisp-macos-arm64.tar.gz", "wchisp"

    raise ProvisioningError(
        "No official prebuilt wchisp nightly binary is available for "
        f"{platform.system()} {platform.machine()}. Install wchisp manually and "
        "pass its path with --wchisp."
    )


def dependency_hint() -> str:
    system = platform.system().strip().lower()
    if system == "darwin":
        return " On macOS, install libusb first with: brew install libusb"
    if system == "linux":
        return (
            " On Debian/Ubuntu, install the libusb runtime if necessary with: "
            "sudo apt install libusb-1.0-0"
        )
    if system == "windows":
        return (
            " On Windows, the Microsoft VC runtime and a compatible WCH/WinUSB "
            "driver may be required."
        )
    return ""


def download_to_file(url: str, output: BinaryIO) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "gbflash-provisioning/1.0",
            "Accept": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise ProvisioningError(
            f"Could not download wchisp: HTTP {exc.code} from {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProvisioningError(
            f"Could not download wchisp from {url}: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise ProvisioningError(f"Could not save downloaded wchisp archive: {exc}") from exc


def extract_executable(archive_path: Path, asset_name: str, executable_name: str, destination: Path) -> None:
    """Extract only the expected executable, ignoring all other archive members."""
    temporary_destination = destination.with_name(destination.name + ".tmp")
    try:
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                matches = [
                    member
                    for member in archive.infolist()
                    if not member.is_dir()
                    and PurePosixPath(member.filename).name.lower() == executable_name.lower()
                ]
                if len(matches) != 1:
                    raise ProvisioningError(
                        f"Downloaded archive contains {len(matches)} matching "
                        f"'{executable_name}' files; expected exactly one"
                    )
                with archive.open(matches[0]) as source, temporary_destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        elif asset_name.endswith(".tar.gz"):
            with tarfile.open(archive_path, mode="r:gz") as archive:
                matches = [
                    member
                    for member in archive.getmembers()
                    if member.isfile()
                    and PurePosixPath(member.name).name.lower() == executable_name.lower()
                ]
                if len(matches) != 1:
                    raise ProvisioningError(
                        f"Downloaded archive contains {len(matches)} matching "
                        f"'{executable_name}' files; expected exactly one"
                    )
                source = archive.extractfile(matches[0])
                if source is None:
                    raise ProvisioningError(
                        f"Could not extract '{executable_name}' from downloaded archive"
                    )
                with source, temporary_destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            raise ProvisioningError(f"Unsupported wchisp archive format: {asset_name}")

        if os.name != "nt":
            temporary_destination.chmod(
                temporary_destination.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
        os.replace(temporary_destination, destination)
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ProvisioningError(f"Downloaded wchisp archive is invalid: {exc}") from exc
    except OSError as exc:
        raise ProvisioningError(f"Could not install downloaded wchisp: {exc}") from exc
    finally:
        try:
            temporary_destination.unlink(missing_ok=True)
        except OSError:
            pass


def verify_wchisp_executable(executable: Path) -> None:
    try:
        result = subprocess.run(
            [str(executable), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError(
            f"Downloaded wchisp could not be started: {exc}.{dependency_hint()}"
        ) from exc

    if result.returncode != 0:
        output = (result.stdout or "no output").strip()
        raise ProvisioningError(
            "Downloaded wchisp failed its startup check."
            f"{dependency_hint()}\n\n{output}"
        )


def download_wchisp(tools_dir: Path) -> str:
    platform_key, asset_name, executable_name = wchisp_platform_asset()
    install_dir = tools_dir.expanduser().resolve() / f"wchisp-{platform_key}"
    executable = install_dir / executable_name

    if executable.is_file():
        if os.name != "nt":
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        verify_wchisp_executable(executable)
        print(f"Using cached wchisp: {executable}")
        return str(executable)

    install_dir.mkdir(parents=True, exist_ok=True)
    url = f"{WCHISP_RELEASE_BASE_URL}/{asset_name}"
    print("wchisp was not found in PATH.")
    print(f"Downloading official {WCHISP_RELEASE_TAG} build: {asset_name}")
    print(f"Source: {url}")

    archive_suffix = ".zip" if asset_name.endswith(".zip") else ".tar.gz"
    archive_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix="wchisp-",
            suffix=archive_suffix,
            dir=install_dir,
            delete=False,
        ) as temporary_archive:
            archive_path = Path(temporary_archive.name)
            download_to_file(url, temporary_archive)
        extract_executable(archive_path, asset_name, executable_name, executable)
    finally:
        if archive_path is not None:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass

    verify_wchisp_executable(executable)
    print(f"Installed wchisp: {executable}")
    return str(executable)


def resolve_wchisp(requested: str, tools_dir: Path, allow_download: bool) -> str:
    requested_path = Path(requested).expanduser()
    if requested_path.is_file():
        return str(requested_path.resolve())

    found = shutil.which(requested)
    if found is not None:
        return found

    # Auto-download only for the default command name. A missing explicit custom
    # path is probably a typo and should not silently select a different binary.
    if requested not in {"wchisp", "wchisp.exe"}:
        raise ProvisioningError(f"wchisp executable not found: {requested}")
    if not allow_download:
        raise ProvisioningError(
            "wchisp was not found in PATH and automatic download is disabled"
        )

    return download_wchisp(tools_dir)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uid_text(uid: bytes) -> str:
    return "-".join(f"{byte:02X}" for byte in uid)


def uid_slug(uid: bytes) -> str:
    return uid.hex()


def command_text(command: Sequence[str]) -> str:
    return " ".join(command)


def run_command(
    command: Sequence[str],
    *,
    check: bool = True,
    timeout: float = 30.0,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print(f"$ {command_text(command)}")

    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProvisioningError(f"Command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProvisioningError(
            f"Command timed out after {timeout:g} seconds: {command_text(command)}"
        ) from exc

    if not quiet and result.stdout:
        print(result.stdout.rstrip())

    if check and result.returncode != 0:
        output = result.stdout.strip() or "no output"
        raise ProvisioningError(
            f"Command failed with exit code {result.returncode}:\n"
            f"  {command_text(command)}\n\n{output}"
        )

    return result


class Wchisp:
    def __init__(self, executable: str, device_index: int | None) -> None:
        self.executable = executable
        self.global_args: list[str] = []
        if device_index is not None:
            self.global_args.extend(["--device", str(device_index)])

    def command(self, *args: str) -> list[str]:
        # wchisp global options must precede the subcommand.
        return [self.executable, *self.global_args, *args]

    def info(self) -> str:
        result = run_command(self.command("info", "--chip", "CH579"))
        return result.stdout

    def probe(self) -> tuple[bool, str]:
        result = run_command(
            self.command("probe"), check=False, timeout=10.0, quiet=True
        )
        output = result.stdout or ""
        present = result.returncode == 0 and re.search(
            r"Device\s+#\d+\s*:\s*CH579\b", output, flags=re.IGNORECASE
        ) is not None
        return present, output

    def dump_eeprom(self, output_path: Path) -> None:
        run_command(self.command("eeprom", "dump", str(output_path)))

    def write_eeprom(self, image_path: Path) -> subprocess.CompletedProcess[str]:
        return run_command(
            self.command("eeprom", "write", str(image_path)),
            check=False,
            timeout=90.0,
        )


def parse_uid(info_output: str) -> bytes:
    match = re.search(
        r"Chip\s+UID\s*:\s*"
        r"([0-9A-Fa-f]{2}(?:\s*[-:]\s*[0-9A-Fa-f]{2}){7})",
        info_output,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ProvisioningError("Could not find an 8-byte 'Chip UID:' in wchisp info output")

    parts = re.findall(r"[0-9A-Fa-f]{2}", match.group(1))
    uid = bytes(int(part, 16) for part in parts)
    if len(uid) != UID_SIZE:
        raise ProvisioningError(f"Expected an {UID_SIZE}-byte UID, got {len(uid)} bytes")
    return uid


def create_credential(uid: bytes) -> bytes:
    """Create two RSA blocks from big-endian 3-byte messages.

    Each signature integer is serialized as one little-endian 32-bit EEPROM block.
    """
    if len(uid) != UID_SIZE:
        raise ProvisioningError(f"Expected an {UID_SIZE}-byte UID, got {len(uid)} bytes")

    # Firmware-visible message: UID[0:5], followed by a copy of UID[0].
    message = uid[:5] + uid[:1]
    record = bytearray()

    for offset in (0, 3):
        # The three UID bytes form a big-endian message integer.
        message_integer = int.from_bytes(message[offset : offset + 3], "big")
        if message_integer >= N:
            raise ProvisioningError("UID-derived message block is outside the RSA modulus")
        signature_integer = pow(message_integer, D, N)
        record.extend(signature_integer.to_bytes(4, "little"))

    credential = bytes(record)
    if len(credential) != CREDENTIAL_SIZE:
        raise AssertionError("internal error: credential is not 8 bytes")
    return credential


def validate_credential(uid: bytes, credential: bytes) -> None:
    if len(credential) != CREDENTIAL_SIZE:
        raise ProvisioningError(
            f"Credential must be {CREDENTIAL_SIZE} bytes, got {len(credential)}"
        )

    expected = uid[:5] + uid[:1]
    decoded_blocks: list[bytes] = []
    for block_index, offset in enumerate((0, 4)):
        signature_integer = int.from_bytes(credential[offset : offset + 4], "little")
        decoded_integer = pow(signature_integer, E, N)
        expected_block = expected[block_index * 3 : block_index * 3 + 3]
        expected_integer = int.from_bytes(expected_block, "big")
        if decoded_integer != expected_integer:
            raise ProvisioningError(
                "Generated credential failed its local public-key verification: "
                f"block {block_index} decoded=0x{decoded_integer:X}, "
                f"expected=0x{expected_integer:X}"
            )
        decoded_blocks.append(decoded_integer.to_bytes(3, "big"))

    if b"".join(decoded_blocks) != expected:
        raise AssertionError("internal error: decoded credential bytes do not match UID message")


def read_exact(path: Path, expected_size: int, description: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProvisioningError(f"Could not read {description} '{path}': {exc}") from exc

    if len(data) != expected_size:
        raise ProvisioningError(
            f"{description} has the wrong size: expected {expected_size} bytes, "
            f"got {len(data)} bytes ({path})"
        )
    return data


def first_difference(expected: bytes, actual: bytes) -> str:
    common_length = min(len(expected), len(actual))
    for offset in range(common_length):
        if expected[offset] != actual[offset]:
            return (
                f"first mismatch at EEPROM offset 0x{offset:04X}: "
                f"expected 0x{expected[offset]:02X}, read 0x{actual[offset]:02X}"
            )
    if len(expected) != len(actual):
        return f"length mismatch: expected {len(expected)}, read {len(actual)}"
    return "no difference"


def wait_for_ch579(wchisp: Wchisp, timeout_seconds: float, poll_seconds: float) -> None:
    print("Waiting for a CH579 in WCH ISP mode", end="", flush=True)
    started = time.monotonic()

    while True:
        present, _output = wchisp.probe()
        if present:
            print(" found.")
            return

        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            print()
            raise ProvisioningError(
                f"No CH579 appeared in ISP mode within {timeout_seconds:g} seconds"
            )

        print(".", end="", flush=True)
        time.sleep(poll_seconds)


def command_failure(result: subprocess.CompletedProcess[str], command: Sequence[str]) -> ProvisioningError:
    output = result.stdout.strip() or "no output"
    return ProvisioningError(
        f"Command failed with exit code {result.returncode}:\n"
        f"  {command_text(command)}\n\n{output}"
    )


def write_eeprom_with_reconnect(
    wchisp: Wchisp,
    image_path: Path,
    expected_uid: bytes,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    """Write EEPROM and recover from the CH579 0x00 device-type error.

    Some devices disappear or leave ISP mode between the preceding dump and the
    write command.  When wchisp reports "Device type of 0x00 not found", ask
    the operator to reconnect the target in ISP mode, wait for it, confirm that
    the same UID returned, and retry the write.
    """
    command = wchisp.command("eeprom", "write", str(image_path))

    while True:
        result = wchisp.write_eeprom(image_path)
        if result.returncode == 0:
            return

        output = result.stdout or ""
        if re.search(r"Device\s+type\s+of\s+0x00\s+not\s+found", output, re.IGNORECASE) is None:
            raise command_failure(result, command)

        print(
            "\nThe CH579 is no longer available in ISP mode. "
            "Disconnect/power-cycle it and reconnect it in WCH ISP mode."
        )
        try:
            input("Press Enter after reconnecting; I will wait for the device... ")
        except EOFError:
            print("No interactive input available; waiting immediately.")

        wait_for_ch579(wchisp, timeout_seconds, poll_seconds)

        print("Checking that the same physical device returned...")
        returned_uid = parse_uid(wchisp.info())
        if returned_uid != expected_uid:
            raise ProvisioningError(
                "A different CH579 was connected before the EEPROM write retry: "
                f"expected {uid_text(expected_uid)}, got {uid_text(returned_uid)}"
            )

        print("Retrying the EEPROM write...")


def verify_key_parameters() -> None:
    if P * Q != N:
        raise ProvisioningError("Embedded RSA key is inconsistent: p * q != n")

    # The supplied d is an inverse of e modulo lcm(p - 1, q - 1).
    from math import gcd

    lambda_n = ((P - 1) * (Q - 1)) // gcd(P - 1, Q - 1)
    if (E * D) % lambda_n != 1:
        raise ProvisioningError("Embedded RSA key is inconsistent: e * d mod lambda(n) != 1")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate, flash, and verify a UID-specific CH579 EEPROM image"
    )
    parser.add_argument(
        "--wchisp",
        default="wchisp",
        help="wchisp executable or path (default: %(default)s)",
    )
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=Path(".gbflash-tools"),
        help=(
            "cache directory for an automatically downloaded wchisp "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--no-wchisp-download",
        action="store_true",
        help="fail instead of downloading wchisp when it is not found",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="optional wchisp USB device index",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("provisioning-output"),
        help="directory for backup, generated image, and readback (default: %(default)s)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for ISP re-entry; 0 waits indefinitely (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds between wchisp probe attempts (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.wait_timeout < 0:
        raise ProvisioningError("--wait-timeout cannot be negative")
    if args.poll_interval <= 0:
        raise ProvisioningError("--poll-interval must be greater than zero")
    resolved_wchisp = resolve_wchisp(
        args.wchisp,
        args.tools_dir,
        allow_download=not args.no_wchisp_download,
    )

    verify_key_parameters()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    wchisp = Wchisp(resolved_wchisp, args.device)

    print("[1/7] Querying connected CH579 and reading UID")
    initial_uid = parse_uid(wchisp.info())
    print(f"UID: {uid_text(initial_uid)}")

    prefix = f"uid-{uid_slug(initial_uid)}"
    original_path = args.output_dir / f"{prefix}-original-eeprom.bin"
    image_path = args.output_dir / f"{prefix}-provisioned-eeprom.bin"
    readback_path = args.output_dir / f"{prefix}-readback-eeprom.bin"
    credential_path = args.output_dir / f"{prefix}-credential.bin"

    print("\n[2/7] Backing up the complete 2 KiB EEPROM")
    wchisp.dump_eeprom(original_path)
    original = read_exact(original_path, EEPROM_SIZE, "original EEPROM dump")
    print(f"Backup: {original_path}")
    print(f"SHA-256: {sha256(original)}")

    print("\n[3/7] Generating and locally verifying the UID-specific credential")
    credential = create_credential(initial_uid)
    validate_credential(initial_uid, credential)
    credential_path.write_bytes(credential)
    print(f"Credential: {credential.hex(' ').upper()}")
    print(f"Credential file: {credential_path}")

    print("\n[4/7] Building the complete preserved EEPROM image")
    image = bytearray(original)
    image[CREDENTIAL_OFFSET : CREDENTIAL_OFFSET + CREDENTIAL_SIZE] = credential
    image_bytes = bytes(image)
    if len(image_bytes) != EEPROM_SIZE:
        raise AssertionError("internal error: generated EEPROM image is not 2 KiB")
    image_path.write_bytes(image_bytes)
    print(f"Image: {image_path}")
    print(f"Size: {len(image_bytes)} bytes")
    print(f"SHA-256: {sha256(image_bytes)}")

    print("\n[5/7] Flashing the complete EEPROM image")
    # Validate locally before invoking wchisp because wchisp erases before its own
    # file-size check in current versions.
    read_exact(image_path, EEPROM_SIZE, "generated EEPROM image")
    write_eeprom_with_reconnect(
        wchisp,
        image_path,
        initial_uid,
        args.wait_timeout,
        args.poll_interval,
    )

    print("\n[6/7] Re-enter ISP mode")
    print("Disconnect or power-cycle the device, then reconnect it in WCH ISP mode.")
    try:
        input("Press Enter after the device has been removed; I will wait for it to return... ")
    except EOFError:
        print("No interactive input available; waiting immediately.")
    wait_for_ch579(wchisp, args.wait_timeout, args.poll_interval)

    print("Checking that the same physical device returned...")
    returned_uid = parse_uid(wchisp.info())
    if returned_uid != initial_uid:
        raise ProvisioningError(
            "A different CH579 was connected after programming: "
            f"expected {uid_text(initial_uid)}, got {uid_text(returned_uid)}"
        )

    print("\n[7/7] Reading back and comparing the complete EEPROM")
    wchisp.dump_eeprom(readback_path)
    readback = read_exact(readback_path, EEPROM_SIZE, "EEPROM readback")

    if readback != image_bytes:
        raise ProvisioningError(
            "EEPROM verification failed: "
            f"{first_difference(image_bytes, readback)}\n"
            f"Expected SHA-256: {sha256(image_bytes)}\n"
            f"Readback SHA-256: {sha256(readback)}\n"
            f"Original backup remains at: {original_path}\n"
            f"Readback remains at: {readback_path}"
        )

    print("EEPROM verification successful.")
    print(f"UID: {uid_text(initial_uid)}")
    print(f"Verified SHA-256: {sha256(readback)}")
    print(f"Original backup: {original_path}")
    print(f"Programmed image: {image_path}")
    print(f"Readback image: {readback_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        raise SystemExit(130)
    except ProvisioningError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
