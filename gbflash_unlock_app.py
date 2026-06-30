#!/usr/bin/env python3
"""GBFlash Unlock Qt6 GUI."""

from __future__ import annotations

import sys
import inspect
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

import gbflash_ch579_isp as isp
import gbflash_serial_update as serial_update
import gbflash_unlock_backend as backend


APP_NAME = "GBFlash Unlock"


def provisioning_output_dir() -> Path:
    location = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.AppDataLocation
    )
    if location:
        base = Path(location)
        if base.name != "Python":
            return base / "provisioning-output"
    return Path.home() / "Library" / "Application Support" / APP_NAME / "provisioning-output"


class Worker(QtCore.QObject):
    log = QtCore.Signal(str)
    prompt_requested = QtCore.Signal(str, object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.kwargs = kwargs
        if "prompt" in inspect.signature(fn).parameters:
            self.kwargs.setdefault("prompt", self.prompt_user)

    def prompt_user(self, message: str) -> None:
        event = threading.Event()
        self.prompt_requested.emit(message, event)
        event.wait()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self.fn(log=self.log.emit, **self.kwargs)
        except Exception as exc:  # GUI boundary: surface concise message plus log detail.
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.thread: QtCore.QThread | None = None
        self.worker: Worker | None = None
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(760, 560)
        self.setWindowIcon(self.make_icon())
        self.build_ui()

    def make_icon(self) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(256, 256)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        gradient = QtGui.QLinearGradient(0, 0, 256, 256)
        gradient.setColorAt(0, QtGui.QColor("#23d5ab"))
        gradient.setColorAt(1, QtGui.QColor("#2457ff"))
        painter.setBrush(QtGui.QBrush(gradient))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawRoundedRect(18, 18, 220, 220, 46, 46)
        painter.setPen(QtGui.QPen(QtGui.QColor("white"), 16, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap))
        painter.drawArc(72, 60, 112, 112, 20 * 16, 220 * 16)
        painter.drawRoundedRect(66, 122, 124, 78, 18, 18)
        painter.setBrush(QtGui.QColor("white"))
        painter.drawEllipse(118, 146, 20, 20)
        painter.drawRoundedRect(125, 160, 6, 26, 3, 3)
        painter.end()
        return QtGui.QIcon(pixmap)

    def build_ui(self) -> None:
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        logo = QtWidgets.QLabel()
        logo.setPixmap(self.make_icon().pixmap(72, 72))
        header.addWidget(logo)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(APP_NAME)
        title_font = title.font()
        title_font.setPointSize(24)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QtWidgets.QLabel("Provision EEPROM through CH579 WCH ISP, then flash fw.bin through GBFlash serial update.")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        layout.addLayout(header)

        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        bootloader_row = QtWidgets.QHBoxLayout()
        self.bootloader_edit = QtWidgets.QLineEdit()
        self.bootloader_edit.setPlaceholderText("Select bootloader ISP image (.bin or .hex)")
        browse_bootloader = QtWidgets.QPushButton("Browse...")
        browse_bootloader.clicked.connect(self.browse_bootloader)
        bootloader_row.addWidget(self.bootloader_edit, 1)
        bootloader_row.addWidget(browse_bootloader)
        form.addRow("Bootloader", bootloader_row)

        firmware_row = QtWidgets.QHBoxLayout()
        self.firmware_edit = QtWidgets.QLineEdit()
        self.firmware_edit.setPlaceholderText("Select serial fw.bin or zip containing fw.bin")
        browse_firmware = QtWidgets.QPushButton("Browse...")
        browse_firmware.clicked.connect(self.browse_firmware)
        firmware_row.addWidget(self.firmware_edit, 1)
        firmware_row.addWidget(browse_firmware)
        form.addRow("Firmware", firmware_row)

        key_row = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setPlaceholderText("8 bytes, for example: 12 34 56 78 9A BC DE F0")
        generate = QtWidgets.QPushButton("Generate")
        generate.clicked.connect(self.generate_key)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(generate)
        form.addRow("Unlock key", key_row)

        layout.addLayout(form)

        self.advanced_toggle = QtWidgets.QCheckBox("Advanced")
        layout.addWidget(self.advanced_toggle)

        self.advanced_group = QtWidgets.QGroupBox("Manual device selection")
        advanced_form = QtWidgets.QFormLayout(self.advanced_group)
        advanced_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.usb_selector_edit = QtWidgets.QLineEdit("auto")
        self.usb_selector_edit.setPlaceholderText("auto or VID:PID[:bus:address], for example 4348:55e0")
        advanced_form.addRow("USB selector", self.usb_selector_edit)

        self.device_spin = QtWidgets.QSpinBox()
        self.device_spin.setRange(-1, 64)
        self.device_spin.setValue(-1)
        self.device_spin.setSpecialValueText("auto")
        advanced_form.addRow("USB device", self.device_spin)

        self.serial_port_edit = QtWidgets.QLineEdit("auto")
        self.serial_port_edit.setPlaceholderText("auto or serial port, for example /dev/cu.usbserial-110")
        advanced_form.addRow("Serial port", self.serial_port_edit)

        self.advanced_group.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced_group.setVisible)
        layout.addWidget(self.advanced_group)

        button_row = QtWidgets.QHBoxLayout()
        self.unlock_button = QtWidgets.QPushButton("Unlock && Flash")
        self.unlock_button.clicked.connect(self.unlock_and_flash)
        clear = QtWidgets.QPushButton("Clear Log")
        clear.clicked.connect(lambda: self.log_edit.clear())
        button_row.addStretch(1)
        button_row.addWidget(clear)
        button_row.addWidget(self.unlock_button)
        layout.addLayout(button_row)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("Progress output")
        layout.addWidget(self.log_edit, 1)

        self.statusBar().showMessage("Ready")
        self.setCentralWidget(root)

    def browse_bootloader(self) -> None:
        filename, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Choose bootloader image",
            str(Path.cwd()),
            "Bootloader images (*.bin *.hex);;All files (*)",
        )
        if filename:
            self.bootloader_edit.setText(filename)

    def browse_firmware(self) -> None:
        filename, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Choose serial firmware package",
            str(Path.cwd()),
            "Firmware packages (*.bin *.zip);;All files (*)",
        )
        if filename:
            self.firmware_edit.setText(filename)

    def common_kwargs(self) -> dict[str, Any]:
        if not self.advanced_toggle.isChecked():
            return {
                "usb_selector": "auto",
                "device_index": None,
                "serial_port": "auto",
            }
        device_index = self.device_spin.value()
        return {
            "usb_selector": self.usb_selector_edit.text().strip(),
            "device_index": None if device_index < 0 else device_index,
            "serial_port": self.serial_port_edit.text().strip() or "auto",
        }

    def append_log(self, message: str) -> None:
        self.log_edit.appendPlainText(message.rstrip())

    def set_busy(self, busy: bool) -> None:
        for widget in [
            self.bootloader_edit,
            self.firmware_edit,
            self.key_edit,
            self.advanced_toggle,
            self.usb_selector_edit,
            self.device_spin,
            self.serial_port_edit,
            self.unlock_button,
        ]:
            widget.setEnabled(not busy)
        self.statusBar().showMessage("Working..." if busy else "Ready")

    def run_worker(self, fn: Callable[..., Any], on_success: Callable[[Any], None], **kwargs: Any) -> None:
        if self.thread is not None:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "An operation is already running.")
            return
        self.set_busy(True)
        self.thread = QtCore.QThread(self)
        self.worker = Worker(fn, **kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.prompt_requested.connect(self.handle_prompt)
        self.worker.finished.connect(on_success)
        self.worker.failed.connect(self.operation_failed)
        self.worker.finished.connect(lambda _result: self.cleanup_worker())
        self.worker.failed.connect(lambda _msg: self.cleanup_worker())
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()

    @QtCore.Slot(str, object)
    def handle_prompt(self, message: str, event: object) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setText(message)
        box.setInformativeText(
            "Reconnect the device. This prompt closes automatically once the device is detected, "
            "after it has disappeared and reappeared, or click Continue after reconnecting."
        )
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        box.button(QtWidgets.QMessageBox.StandardButton.Ok).setText("Continue")
        box.setModal(True)

        done = {"value": False}
        seen_absent = {"value": False}
        timer = QtCore.QTimer(box)
        timer.setInterval(1000)

        def finish() -> None:
            if done["value"]:
                return
            done["value"] = True
            timer.stop()
            if hasattr(event, "set"):
                event.set()

        def detected() -> bool:
            text = message.lower()
            try:
                if "serial" in text:
                    return bool(serial_update.autodetect_ports())
                return bool(isp.list_isp_devices())
            except Exception:
                return False

        def poll() -> None:
            is_detected = detected()
            if not is_detected:
                seen_absent["value"] = True
                return
            if seen_absent["value"]:
                self.append_log("Reconnect detected; continuing.")
                finish()
                box.accept()

        box.finished.connect(lambda _result: finish())
        timer.timeout.connect(poll)
        timer.start()
        box.open()
        QtCore.QTimer.singleShot(0, poll)

    @QtCore.Slot()
    def cleanup_worker(self) -> None:
        if self.thread is None:
            return
        self.thread.quit()
        self.thread.wait()
        self.worker = None
        self.thread = None
        self.set_busy(False)

    @QtCore.Slot(str)
    def operation_failed(self, message: str) -> None:
        self.append_log(f"ERROR: {message}")
        QtWidgets.QMessageBox.critical(self, APP_NAME, message)

    def generate_key(self) -> None:
        self.append_log("Generating unlock key...")
        kwargs = self.common_kwargs()
        kwargs.pop("serial_port", None)
        self.run_worker(backend.generate_unlock_key, self.generated_key, **kwargs)

    @QtCore.Slot(object)
    def generated_key(self, result: object) -> None:
        _uid, credential = result
        self.key_edit.setText(backend.format_credential(credential))
        self.append_log("Unlock key populated.")
        QtWidgets.QMessageBox.information(self, APP_NAME, "Unlock key generated from connected device UID.")

    def unlock_and_flash(self) -> None:
        try:
            credential = backend.normalize_credential(self.key_edit.text())
        except backend.UnlockError as exc:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(exc))
            return
        bootloader = Path(self.bootloader_edit.text().strip()).expanduser()
        if not bootloader.is_file():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Select a bootloader image first.")
            return
        firmware = Path(self.firmware_edit.text().strip()).expanduser()
        if not firmware.is_file():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Select a serial firmware package first.")
            return
        kwargs = self.common_kwargs()
        kwargs.update(
            {
                "bootloader": bootloader,
                "firmware": firmware,
                "credential": credential,
                "output_dir": provisioning_output_dir(),
                "wait_timeout": 120.0,
                "poll_interval": 1.0,
                "flash_timeout": 180.0,
            }
        )
        self.append_log("Starting unlock and firmware flash...")
        self.run_worker(backend.unlock_and_flash, self.unlock_flash_done, **kwargs)

    @QtCore.Slot(object)
    def unlock_flash_done(self, result: object) -> None:
        uid = result
        self.append_log(f"Completed for UID: {uid.hex(' ').upper()}")
        QtWidgets.QMessageBox.information(self, APP_NAME, "Unlock and firmware flash completed.")


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
