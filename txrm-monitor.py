#!/usr/bin/env python

"""
txrm-monitor.py

A PySide6 GUI application that monitors directories for .txrm files and
automatically extracts metadata when files are stable.

By Hollister Herhold, AMNH, 2026.

This application was developed using Claude Sonnet 4.5 using the REQUIREMENTS.md file
as a guide for features and functionality. 

"""

import sys
import os
import json
import logging
import time
from pathlib import Path
from datetime import datetime
from threading import Thread, Lock
from typing import Dict, List, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QFileDialog,
    QLabel, QListWidget, QMessageBox, QHeaderView, QStatusBar,
    QDialog, QDialogButtonBox, QCheckBox, QSpinBox, QGroupBox,
    QScrollArea, QLineEdit, QFormLayout,
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QTextCursor

import struct
import olefile


# ---------------------------------------------------------------------------
# OLE metadata helpers (no xrmreader dependency)
# ---------------------------------------------------------------------------

# All ImageInfo fields to read, keyed by OLE path.
# Value tuple: (struct_fmt, 'value') for numeric streams,
#              (None, 'string') for text streams.
# For per-image array streams the first element is read via struct.unpack_from.
# Fields present only in .txrm return None when the file is a .txm.
_IMAGEINFO_FIELDS = {
    # -- common to .txm and .txrm --
    'ImageInfo/AcquisitionMode':        ('<I', 'value'),
    'ImageInfo/CamFullHeight':          ('<I', 'value'),
    'ImageInfo/CamFullWidth':           ('<I', 'value'),
    'ImageInfo/CamPixelSize':           ('<f', 'value'),
    'ImageInfo/CameraBinning':          ('<I', 'value'),
    'ImageInfo/CameraFineRotation':     ('<f', 'value'),
    'ImageInfo/CameraName':             (None, 'string'),
    'ImageInfo/CameraNo':               ('<I', 'value'),
    'ImageInfo/CameraOffset':           ('<f', 'value'),
    'ImageInfo/CameraTemperature':      ('<f', 'value'),
    'ImageInfo/CameraType':             (None, 'string'),
    'ImageInfo/ConeAngle':              ('<f', 'value'),
    'ImageInfo/Current':                ('<f', 'value'),
    'ImageInfo/DataType':               ('<I', 'value'),
    'ImageInfo/Date':                   (None, 'string'),
    'ImageInfo/DtoRADistance':          ('<f', 'value'),
    'ImageInfo/Energy':                 ('<f', 'value'),
    'ImageInfo/ExpTimes':               ('<f', 'value'),
    'ImageInfo/FanAngle':               ('<f', 'value'),
    'ImageInfo/FileType':               ('<I', 'value'),
    'ImageInfo/FocusTarget':            ('<f', 'value'),
    'ImageInfo/FramesAveraged':         ('<I', 'value'),
    'ImageInfo/FramesPerImage':         ('<I', 'value'),
    'ImageInfo/HorizontalBin':          ('<I', 'value'),
    'ImageInfo/ImageHeight':            ('<I', 'value'),
    'ImageInfo/ImageWidth':             ('<I', 'value'),
    'ImageInfo/ImagesPerProjection':    ('<I', 'value'),
    'ImageInfo/ImagesTaken':            ('<I', 'value'),
    'ImageInfo/IonChamberCurrent':      ('<f', 'value'),
    'ImageInfo/IsContMotion':           ('<I', 'value'),
    'ImageInfo/IsFlatPanel':            ('<I', 'value'),
    'ImageInfo/NoOfImages':             ('<I', 'value'),
    'ImageInfo/NoOfImagesAveraged':     ('<I', 'value'),
    'ImageInfo/ObjectiveID':            ('<I', 'value'),
    'ImageInfo/ObjectiveName':          (None, 'string'),
    'ImageInfo/OpticalMagnification':   ('<f', 'value'),
    'ImageInfo/PixelSize':              ('<f', 'value'),
    'ImageInfo/RefInterval':            ('<I', 'value'),
    'ImageInfo/SourceFilterID':         ('<I', 'value'),
    'ImageInfo/SourceFilterIndex':      ('<I', 'value'),
    'ImageInfo/SourceFilterName':       (None, 'string'),
    'ImageInfo/StoRADistance':          ('<f', 'value'),
    'ImageInfo/SystemType':             (None, 'string'),
    'ImageInfo/Temperature':            ('<f', 'value'),
    'ImageInfo/VerticalalBin':          ('<I', 'value'),
    'ImageInfo/Voltage':                ('<f', 'value'),
    'ImageInfo/XrayCurrent':            ('<f', 'value'),
    'ImageInfo/XrayMagnification':      ('<f', 'value'),
    'ImageInfo/XrayVoltage':            ('<f', 'value'),
    # -- present only in .txrm --
    'ImageInfo/AutoGridOn':             ('<I', 'value'),
    'ImageInfo/CCFilAdjustStep':        ('<f', 'value'),
    'ImageInfo/CCVersion':              (None, 'string'),
    'ImageInfo/ColdCathodeState':       ('<I', 'value'),
    'ImageInfo/Filament':               (None, 'string'),
    'ImageInfo/FilamentPercent':        ('<f', 'value'),
    'ImageInfo/GridOffset':             ('<f', 'value'),
    'ImageInfo/GridVoltage':            ('<f', 'value'),
    'ImageInfo/IsCCOn':                 ('<I', 'value'),
    'ImageInfo/RequestedFilament':      ('<f', 'value'),
    'ImageInfo/RequestedPower':         ('<f', 'value'),
    'ImageInfo/RequestedTargetCurrent': ('<f', 'value'),
    'ImageInfo/SourceDriftInterval':    ('<I', 'value'),
    'ImageInfo/SourceDriftTotal':       ('<f', 'value'),
    'ImageInfo/SourceSerialNumber':     (None, 'string'),
    'ImageInfo/SourceType':             (None, 'string'),
    'ImageInfo/SpotIndex':              ('<I', 'value'),
    'ImageInfo/TargetTurn':             ('<I', 'value'),
    'ImageInfo/TFMIsOn':                ('<I', 'value'),
    'ImageInfo/TubeEfficiency':         ('<f', 'value'),
    'ImageInfo/TubeState':              ('<I', 'value'),
}

# Default fields written to the output .txt file.
# Objective ID:  3 = 4X,  5 = 20X
_DEFAULT_OUTPUT_FIELDS = [
    # -- common to .txm and .txrm --
    'ImageInfo/ImageWidth',
    'ImageInfo/ImageHeight',
    'ImageInfo/DataType',
    'ImageInfo/NoOfImages',
    'ImageInfo/PixelSize',
    'ImageInfo/ConeAngle',
    'ImageInfo/FanAngle',
    'ImageInfo/CameraOffset',
    'ImageInfo/Current',
    'ImageInfo/Voltage',
    'ImageInfo/ExpTimes',
    'ImageInfo/StoRADistance',
    'ImageInfo/DtoRADistance',
    'ImageInfo/ObjectiveID',
    'ImageInfo/ObjectiveName',
    'ImageInfo/SourceFilterName',
    'ImageInfo/CameraBinning',
    'ImageInfo/CameraName',
    'ImageInfo/Date',
    'ImageInfo/SystemType',
    'ImageInfo/XrayCurrent',
    'ImageInfo/XrayVoltage',
    # -- present only in .txrm (None for .txm files) --
    'ImageInfo/CCVersion',
    'ImageInfo/SourceDriftTotal',
    'ImageInfo/SourceType',
    'ImageInfo/SourceSerialNumber',
    'ImageInfo/Filament',
    'ImageInfo/FilamentPercent',
    'ImageInfo/TubeEfficiency',
    'ImageInfo/TubeState',
]

# Fields present only in .txrm files (used to grey them out when txrm is not selected).
_TXRM_ONLY_FIELDS = {
    'ImageInfo/AutoGridOn',
    'ImageInfo/CCFilAdjustStep',
    'ImageInfo/CCVersion',
    'ImageInfo/ColdCathodeState',
    'ImageInfo/Filament',
    'ImageInfo/FilamentPercent',
    'ImageInfo/GridOffset',
    'ImageInfo/GridVoltage',
    'ImageInfo/IsCCOn',
    'ImageInfo/RequestedFilament',
    'ImageInfo/RequestedPower',
    'ImageInfo/RequestedTargetCurrent',
    'ImageInfo/SourceDriftInterval',
    'ImageInfo/SourceDriftTotal',
    'ImageInfo/SourceSerialNumber',
    'ImageInfo/SourceType',
    'ImageInfo/SpotIndex',
    'ImageInfo/TargetTurn',
    'ImageInfo/TFMIsOn',
    'ImageInfo/TubeEfficiency',
    'ImageInfo/TubeState',
}

DEFAULT_PREFS = {
    'directories': [],
    'scan_txrm': True,
    'scan_txm': False,
    'scan_interval_minutes': 5,
    'stability_minutes': 10,
    'log_dir': 'logs',
    'output_fields': list(_DEFAULT_OUTPUT_FIELDS),
}


def _read_ole_value(ole, path, fmt):
    """Read the first scalar value from an OLE stream using struct format fmt.
    Returns None if the stream does not exist or cannot be unpacked."""
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    try:
        return struct.unpack_from(fmt, data)[0]
    except struct.error:
        return None


def _read_ole_string(ole, path):
    """Read a null-terminated string from an OLE stream.

    Detects UTF-16-LE (alternating zero bytes) and falls back to UTF-8/ASCII.
    Returns None if the stream does not exist or cannot be decoded.
    """
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    if not data:
        return None
    # Detect UTF-16-LE by checking for alternating null bytes at the start.
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        # Find the double-null terminator on an even boundary.
        i = 0
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                break
            i += 2
        try:
            return data[:i].decode('utf-16-le')
        except UnicodeDecodeError:
            pass
    # UTF-8 / ASCII: truncate at the first null byte.
    end = data.find(b'\x00')
    if end >= 0:
        data = data[:end]
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def read_metadata(file_name):
    """Read ImageInfo metadata from a Zeiss .txrm or .txm file.

    Keys in the returned dictionary are OLE paths (e.g. 'ImageInfo/ImageWidth').
    Fields present only in .txrm files return None when a .txm file is read.
    Raises ValueError if the file is not a valid OLE file.
    """
    if not olefile.isOleFile(file_name):
        raise ValueError(f"'{file_name}' is not a valid OLE file.")

    with olefile.OleFileIO(file_name) as ole:
        metadata = {}
        for path, (fmt, reader) in _IMAGEINFO_FIELDS.items():
            if reader == 'string':
                metadata[path] = _read_ole_string(ole, path)
            else:
                metadata[path] = _read_ole_value(ole, path, fmt)

    return metadata


# Constants
STABILITY_CHECK_INTERVAL = 10 * 1000  # 10 seconds in milliseconds (stability polling rate)
CONFIG_FILE = "txrm-monitor-config.json"


class FileMonitorState:
    """Represents the state of a monitored .txrm file"""
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.size = os.path.getsize(filepath)
        self.last_size_change = time.time()
        self.status = "Waiting for changes"
        self.is_processing = False
        self.is_completed = False
        self.error = None
    
    def update_size(self):
        """Check if file size has changed and update state"""
        try:
            current_size = os.path.getsize(self.filepath)
            if current_size != self.size:
                self.size = current_size
                self.last_size_change = time.time()
                return True
        except OSError:
            return False
        return False
    
    def is_stable(self) -> bool:
        """Check if file has been stable for the required duration"""
        return (time.time() - self.last_size_change) >= STABILITY_DURATION
    
    def time_until_stable(self) -> int:
        """Returns seconds until file is considered stable"""
        elapsed = time.time() - self.last_size_change
        remaining = STABILITY_DURATION - elapsed
        return max(0, int(remaining))


class LogSignaler(QObject):
    """Signal emitter for logging to GUI"""
    log_message = Signal(str)


class RotatingFileHandler(logging.Handler):
    """Custom logging handler with daily rotation"""
    def __init__(self, log_dir: str):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.current_date = None
        self.file_handler = None
        self._rotate_if_needed()
    
    def _rotate_if_needed(self):
        """Rotate log file if date has changed"""
        today = datetime.now().date()
        if today != self.current_date:
            if self.file_handler:
                self.file_handler.close()
            self.current_date = today
            log_file = self.log_dir / f"txrm-monitor-{today}.log"
            self.file_handler = open(log_file, 'a', encoding='utf-8')
    
    def emit(self, record):
        """Write log record to file"""
        self._rotate_if_needed()
        msg = self.format(record)
        self.file_handler.write(msg + '\n')
        self.file_handler.flush()


class FileMonitor(QObject):
    """Background file monitoring system"""
    
    status_updated = Signal()
    status_message = Signal(str)
    
    def __init__(self, logger: logging.Logger, file_extensions: set,
                 output_fields: List[str], stability_seconds: int = 600):
        super().__init__()
        self.logger = logger
        self.monitored_files: Dict[str, FileMonitorState] = {}
        self.directories: List[str] = []
        self.file_extensions = file_extensions
        self.output_fields = list(output_fields)
        self.stability_seconds = stability_seconds
        self.lock = Lock()
        self.running = False

    def set_file_extensions(self, extensions: set):
        """Update the set of file extensions to monitor."""
        with self.lock:
            self.file_extensions = extensions

    def set_output_fields(self, fields: List[str]):
        """Update the list of fields to write to output files."""
        with self.lock:
            self.output_fields = list(fields)

    def set_stability_seconds(self, seconds: int):
        """Update the stability duration."""
        self.stability_seconds = seconds

    def set_directories(self, directories: List[str]):
        """Update list of directories to monitor"""
        with self.lock:
            self.directories = directories
            self.logger.info(f"Updated monitored directories: {directories}")
    
    def scan_directories(self):
        """Scan all directories for .txrm files"""
        if not self.directories:
            self.status_message.emit("No directories configured")
            return
        
        self.logger.info("Scanning directories for .txrm files...")
        self.status_message.emit(f"Scanning {len(self.directories)} directories...")
        found_files = set()
        
        for directory in self.directories:
            if not os.path.isdir(directory):
                self.logger.warning(f"Directory not found: {directory}")
                continue
            
            self.status_message.emit(f"Scanning: {directory}")
            
            try:
                # Recursively scan directory and subdirectories
                for root, dirs, files in os.walk(directory):
                    self.status_message.emit(f"Scanning: {root}")
                    
                    for filename in files:
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in self.file_extensions:
                            txrm_path = os.path.join(root, filename)
                            txt_path = txrm_path + '.txt'
                            
                            # Skip if already has metadata file
                            if os.path.exists(txt_path):
                                continue
                            
                            found_files.add(txrm_path)
                            
                            # Add to monitoring if new
                            with self.lock:
                                if txrm_path not in self.monitored_files:
                                    self.monitored_files[txrm_path] = FileMonitorState(txrm_path)
                                    self.logger.info(f"New file detected: {txrm_path}")
            except Exception as e:
                self.logger.error(f"Error scanning directory {directory}: {e}")
        
        # Remove files that no longer exist or have been processed
        with self.lock:
            to_remove = []
            for filepath in self.monitored_files.keys():
                if filepath not in found_files:
                    to_remove.append(filepath)
            
            for filepath in to_remove:
                del self.monitored_files[filepath]
                self.logger.info(f"Removed from monitoring: {filepath}")
        
        self.logger.info(f"Scan complete. Monitoring {len(self.monitored_files)} files")
        self.status_message.emit(f"Scan complete. Monitoring {len(self.monitored_files)} files")
        self.status_updated.emit()
    
    def check_stability_and_process(self):
        """Check file stability and process stable files"""
        with self.lock:
            files_to_check = list(self.monitored_files.items())
        
        if files_to_check:
            self.status_message.emit(f"Checking stability of {len(files_to_check)} files...")
        
        any_changed = False
        for filepath, state in files_to_check:
            if state.is_processing or state.is_completed:
                continue
            
            # Update file size
            size_changed = state.update_size()
            if size_changed:
                self.logger.info(f"File size changed: {filepath}")
                state.status = f"Waiting for changes ({state.time_until_stable()}s)"
                any_changed = True
                continue
            
            # Check if stable
            elapsed = time.time() - state.last_size_change
            if elapsed >= self.stability_seconds:
                self.logger.info(f"File is stable, processing: {filepath}")
                self.status_message.emit(f"Processing: {os.path.basename(filepath)}")
                state.is_processing = True
                state.status = "Processing"
                any_changed = True

                # Process in background thread
                Thread(target=self._process_file, args=(filepath, state), daemon=True).start()
            else:
                remaining = max(0, int(self.stability_seconds - elapsed))
                state.status = f"Waiting for changes ({remaining}s)"
                any_changed = True
        
        if any_changed:
            self.status_updated.emit()
    
    def _process_file(self, filepath: str, state: FileMonitorState):
        """Extract metadata from file"""
        txt_path = filepath + '.txt'
        
        try:
            self.logger.info(f"Extracting metadata from: {filepath}")
            
            # Extract metadata directly via OLE calls
            metadata = read_metadata(filepath)

            # Format metadata output (similar to get-metadata-from-txrm.py)
            output_lines = []
            output_lines.append("txrm-monitor v1.0")
            output_lines.append(f"Metadata extracted from: {filepath}")
            output_lines.append(f"Extraction date: {datetime.now()}")
            output_lines.append("")
            
            for field in self.output_fields:
                value = metadata.get(field, None)
                if value is not None:
                    output_lines.append(f"{field}: {value}")
                else:
                    output_lines.append(f"{field}: Not found in metadata")
            
            # Write metadata to file
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(output_lines))
            
            state.status = "Completed"
            state.is_completed = True
            state.is_processing = False
            self.logger.info(f"Successfully processed: {filepath}")
            self.status_message.emit(f"Completed: {os.path.basename(filepath)}")
            
        except Exception as e:
            # Write error file
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write("ERROR PROCESSING METADATA\n")
                f.write(f"Error: {str(e)}\n")
                f.write(f"File: {filepath}\n")
                f.write(f"Date: {datetime.now()}\n")
            
            state.status = "Error"
            state.error = str(e)
            state.is_completed = True  # Mark as completed so it's no longer monitored
            state.is_processing = False
            self.logger.error(f"Error processing {filepath}: {e}")
            self.status_message.emit(f"Error processing: {os.path.basename(filepath)}: {e}")
        
        self.status_updated.emit()
    
    def get_monitored_files(self) -> List[tuple]:
        """Get list of monitored files and their states"""
        with self.lock:
            return [(fp, state.status, state.error) for fp, state in self.monitored_files.items()]
    
    def process_file_now(self, filepath: str):
        """Force immediate processing of a specific file"""
        with self.lock:
            if filepath not in self.monitored_files:
                return False
            state = self.monitored_files[filepath]
            
            if state.is_processing:
                return False  # Already processing
            
            if state.is_completed:
                return False  # Already completed
        
        self.logger.info(f"Force processing file: {filepath}")
        state.is_processing = True
        state.status = "Processing (forced)"
        self.status_updated.emit()
        
        # Process in background thread
        Thread(target=self._process_file, args=(filepath, state), daemon=True).start()
        return True

    def process_dropped_file(self, filepath: str):
        """Process a drag-and-dropped file immediately, bypassing stability checks.

        If the file is currently being monitored for stability it is removed from the
        monitored list — it no longer needs to be watched since it will be processed now.
        """
        with self.lock:
            existing = self.monitored_files.get(filepath)
            if existing is not None:
                if existing.is_processing:
                    self.logger.info(f"Dropped file already being processed: {filepath}")
                    return False
                if existing.is_completed:
                    self.logger.info(f"Dropped file already processed: {filepath}")
                    return False
                # Remove from monitoring — it will be processed immediately.
                del self.monitored_files[filepath]
                self.logger.info(f"Removed from monitoring (will process via drop): {filepath}")

            state = FileMonitorState(filepath)
            state.is_processing = True
            state.status = "Processing (drag & drop)"
            # Re-insert under the same key so update_file_table can display
            # the processing / completed status while _process_file runs.
            self.monitored_files[filepath] = state

        self.logger.info(f"Processing dropped file: {filepath}")
        self.status_message.emit(f"Processing dropped file: {os.path.basename(filepath)}")
        self.status_updated.emit()

        Thread(target=self._process_file, args=(filepath, state), daemon=True).start()
        return True


class PreferencesDialog(QDialog):
    """Preferences dialog for configuring monitoring settings."""

    def __init__(self, prefs: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(540)
        self.setMinimumHeight(640)
        self._prefs = prefs
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)

        # ── File Types ──────────────────────────────────────────────────────
        ft_group = QGroupBox("File Types to Monitor")
        ft_layout = QVBoxLayout(ft_group)
        self._txrm_cb = QCheckBox("Scan .txrm files")
        self._txrm_cb.setChecked(self._prefs.get('scan_txrm', True))
        self._txm_cb = QCheckBox("Scan .txm files")
        self._txm_cb.setChecked(self._prefs.get('scan_txm', False))
        ft_layout.addWidget(self._txrm_cb)
        ft_layout.addWidget(self._txm_cb)
        self._no_type_warning = QLabel(
            "⚠  No file type selected — the application will not monitor any files."
        )
        self._no_type_warning.setStyleSheet("color: darkorange; font-style: italic;")
        self._no_type_warning.setWordWrap(True)
        ft_layout.addWidget(self._no_type_warning)
        outer.addWidget(ft_group)

        # ── Timing ──────────────────────────────────────────────────────────
        timing_group = QGroupBox("Timing")
        timing_layout = QFormLayout(timing_group)
        self._scan_spin = QSpinBox()
        self._scan_spin.setRange(1, 120)
        self._scan_spin.setSuffix(" min")
        self._scan_spin.setValue(self._prefs.get('scan_interval_minutes', 5))
        timing_layout.addRow("Scan interval:", self._scan_spin)
        self._stab_spin = QSpinBox()
        self._stab_spin.setRange(1, 120)
        self._stab_spin.setSuffix(" min")
        self._stab_spin.setValue(self._prefs.get('stability_minutes', 10))
        timing_layout.addRow("Stability duration:", self._stab_spin)
        outer.addWidget(timing_group)

        # ── Log Directory ────────────────────────────────────────────────────
        log_group = QGroupBox("Log Directory")
        log_layout = QHBoxLayout(log_group)
        self._log_dir_edit = QLineEdit(self._prefs.get('log_dir', 'logs'))
        browse_btn = QPushButton("Browse\u2026")
        browse_btn.clicked.connect(self._browse_log_dir)
        log_layout.addWidget(self._log_dir_edit)
        log_layout.addWidget(browse_btn)
        outer.addWidget(log_group)

        # ── Output Fields ────────────────────────────────────────────────────
        fields_group = QGroupBox("Output Fields")
        fg_layout = QVBoxLayout(fields_group)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        fields_container = QWidget()
        fc_layout = QVBoxLayout(fields_container)
        fc_layout.setSpacing(2)
        enabled_set = set(self._prefs.get('output_fields', list(_DEFAULT_OUTPUT_FIELDS)))
        self._field_cbs = {}
        added_txrm_header = False
        for path in _IMAGEINFO_FIELDS:
            if path in _TXRM_ONLY_FIELDS and not added_txrm_header:
                sep = QLabel("\u2500\u2500 .txrm only \u2500\u2500")
                sep.setStyleSheet("color: gray; font-style: italic; margin-top: 6px;")
                fc_layout.addWidget(sep)
                added_txrm_header = True
            cb = QCheckBox(path)
            cb.setChecked(path in enabled_set)
            self._field_cbs[path] = cb
            fc_layout.addWidget(cb)
        fc_layout.addStretch()
        scroll.setWidget(fields_container)
        fg_layout.addWidget(scroll)
        outer.addWidget(fields_group, stretch=1)

        # Wire txrm checkbox to enable/disable txrm-only fields.
        self._txrm_cb.toggled.connect(self._update_txrm_field_state)
        self._update_txrm_field_state(self._txrm_cb.isChecked())
        # Wire both checkboxes to show/hide the no-type-selected warning.
        self._txrm_cb.toggled.connect(self._update_no_type_warning)
        self._txm_cb.toggled.connect(self._update_no_type_warning)
        self._update_no_type_warning()

        # ── Buttons ──────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _browse_log_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self, "Select Log Directory", self._log_dir_edit.text()
        )
        if directory:
            self._log_dir_edit.setText(directory)

    def _update_txrm_field_state(self, enabled: bool):
        for path, cb in self._field_cbs.items():
            if path in _TXRM_ONLY_FIELDS:
                cb.setEnabled(enabled)
                if not enabled:
                    cb.setChecked(False)

    def _update_no_type_warning(self, _checked: bool = False):
        neither = not self._txrm_cb.isChecked() and not self._txm_cb.isChecked()
        self._no_type_warning.setVisible(neither)

    def get_prefs(self) -> dict:
        """Return updated preferences from the dialog controls."""
        selected = {path for path, cb in self._field_cbs.items() if cb.isChecked()}
        # Preserve _IMAGEINFO_FIELDS insertion order.
        output_fields = [p for p in _IMAGEINFO_FIELDS if p in selected]
        return {
            'scan_txrm': self._txrm_cb.isChecked(),
            'scan_txm': self._txm_cb.isChecked(),
            'scan_interval_minutes': self._scan_spin.value(),
            'stability_minutes': self._stab_spin.value(),
            'log_dir': self._log_dir_edit.text().strip() or 'logs',
            'output_fields': output_fields,
        }


class TXRMMonitorApp(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TXRM File Monitor")
        self.setGeometry(100, 100, 1000, 700)

        # Load config before setting up logging so we know the log directory.
        self.prefs = dict(DEFAULT_PREFS)
        self._load_config_early()

        # Setup logging with the configured (or default) log directory.
        self.setup_logging(self.prefs['log_dir'])
        self.logger.info(
            f"Configuration loaded from {CONFIG_FILE}"
            if os.path.exists(CONFIG_FILE)
            else "No configuration file found; using defaults"
        )

        # Initialize file monitor with preferences.
        self.file_monitor = FileMonitor(
            self.logger,
            file_extensions=self._get_file_extensions(),
            output_fields=self.prefs['output_fields'],
            stability_seconds=self.prefs['stability_minutes'] * 60,
        )
        self.file_monitor.set_directories(self.prefs['directories'])
        self.file_monitor.status_updated.connect(self.update_file_table)
        self.file_monitor.status_message.connect(self.update_status_bar)

        # Setup UI
        self.setup_ui()

        # Setup timers
        scan_interval_ms = self.prefs['scan_interval_minutes'] * 60 * 1000
        self.next_scan_time = time.time() + (scan_interval_ms / 1000)

        self.scan_timer = QTimer()
        self.scan_timer.timeout.connect(self.on_scan_timeout)
        self.scan_timer.start(scan_interval_ms)

        self.stability_timer = QTimer()
        self.stability_timer.timeout.connect(self.file_monitor.check_stability_and_process)
        self.stability_timer.start(STABILITY_CHECK_INTERVAL)

        # Countdown update timer (updates every second)
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.countdown_timer.start(1000)

        # Initial scan
        self.file_monitor.scan_directories()

        self.logger.info("TXRM Monitor application started")
    
    def setup_logging(self, log_dir: str = 'logs'):
        """Setup logging system with daily rotation and GUI display."""
        self.logger = logging.getLogger('TXRMMonitor')
        self.logger.setLevel(logging.INFO)
        # Clear any handlers from a previous setup_logging call.
        self.logger.handlers.clear()

        # Rotating file handler
        Path(log_dir).mkdir(exist_ok=True, parents=True)
        file_handler = RotatingFileHandler(log_dir)
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)

        # GUI handler
        self.log_signaler = LogSignaler()

        class GUIHandler(logging.Handler):
            def __init__(self, signaler):
                super().__init__()
                self.signaler = signaler

            def emit(self, record):
                msg = self.format(record)
                self.signaler.log_message.emit(msg)

        gui_handler = GUIHandler(self.log_signaler)
        gui_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        gui_handler.setFormatter(gui_formatter)
        self.logger.addHandler(gui_handler)
    
    def setup_ui(self):
        """Create the user interface"""
        self.setAcceptDrops(True)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # Directory configuration section
        dir_layout = QHBoxLayout()
        dir_label = QLabel("Monitored Directories:")
        dir_layout.addWidget(dir_label)
        
        self.dir_list = QListWidget()
        self.dir_list.setMaximumHeight(100)
        for directory in self.prefs['directories']:
            self.dir_list.addItem(directory)
        dir_layout.addWidget(self.dir_list)
        
        dir_button_layout = QVBoxLayout()
        add_dir_btn = QPushButton("Add Directory")
        add_dir_btn.clicked.connect(self.add_directory)
        dir_button_layout.addWidget(add_dir_btn)
        
        remove_dir_btn = QPushButton("Remove Selected")
        remove_dir_btn.clicked.connect(self.remove_directory)
        dir_button_layout.addWidget(remove_dir_btn)
        
        dir_layout.addLayout(dir_button_layout)
        layout.addLayout(dir_layout)
        
        # Countdown timer display, scan now button, and preferences button
        scan_control_layout = QHBoxLayout()
        self.countdown_label = QLabel("Next scan in: --:--")
        self.countdown_label.setStyleSheet("font-weight: bold; padding: 5px;")
        scan_control_layout.addWidget(self.countdown_label)

        scan_now_btn = QPushButton("Scan Now")
        scan_now_btn.clicked.connect(self.scan_now)
        scan_control_layout.addWidget(scan_now_btn)

        preferences_btn = QPushButton("Preferences\u2026")
        preferences_btn.clicked.connect(self.show_preferences)
        scan_control_layout.addWidget(preferences_btn)

        scan_control_layout.addStretch()
        layout.addLayout(scan_control_layout)
        
        # Drag-and-drop hint
        dnd_label = QLabel(
            "Drag and drop .txrm / .txm files onto this window to process them immediately."
        )
        dnd_label.setStyleSheet("color: gray; font-style: italic; padding: 2px;")
        dnd_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(dnd_label)

        # File status table
        file_section_layout = QHBoxLayout()
        status_label = QLabel("Monitored Files:")
        file_section_layout.addWidget(status_label)
        file_section_layout.addStretch()
        
        process_selected_btn = QPushButton("Process Selected Now")
        process_selected_btn.clicked.connect(self.process_selected_now)
        file_section_layout.addWidget(process_selected_btn)
        
        layout.addLayout(file_section_layout)
        
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(3)
        self.file_table.setHorizontalHeaderLabels(["File Path", "Status", "Error"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.file_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.file_table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.file_table)
        
        # Log viewer
        log_label = QLabel("Activity Log:")
        layout.addWidget(log_label)
        
        self.log_viewer = QTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setMaximumHeight(200)
        layout.addWidget(self.log_viewer)
        
        # Connect log signaler
        self.log_signaler.log_message.connect(self.append_log)
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
    
    def add_directory(self):
        """Add a directory to monitor"""
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Monitor")
        if directory:
            self.dir_list.addItem(directory)
            directories = [self.dir_list.item(i).text() for i in range(self.dir_list.count())]
            self.prefs['directories'] = directories
            self.file_monitor.set_directories(directories)
            self.save_config()
            self.file_monitor.scan_directories()
            self.next_scan_time = time.time() + self.prefs['scan_interval_minutes'] * 60
    
    def remove_directory(self):
        """Remove selected directory from monitoring"""
        current_item = self.dir_list.currentItem()
        if current_item:
            self.dir_list.takeItem(self.dir_list.row(current_item))
            directories = [self.dir_list.item(i).text() for i in range(self.dir_list.count())]
            self.prefs['directories'] = directories
            self.file_monitor.set_directories(directories)
            self.save_config()
    
    def update_file_table(self):
        """Update the file status table"""
        files = self.file_monitor.get_monitored_files()
        self.file_table.setRowCount(len(files))
        
        for row, (filepath, status, error) in enumerate(files):
            self.file_table.setItem(row, 0, QTableWidgetItem(filepath))
            self.file_table.setItem(row, 1, QTableWidgetItem(status))
            error_text = error if error else ""
            self.file_table.setItem(row, 2, QTableWidgetItem(error_text))
    
    def append_log(self, message: str):
        """Append message to log viewer"""
        self.log_viewer.append(message)
        # Auto-scroll to bottom
        cursor = self.log_viewer.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_viewer.setTextCursor(cursor)
    
    def update_status_bar(self, message: str):
        """Update status bar with current activity"""
        self.status_bar.showMessage(message)
    
    def on_scan_timeout(self):
        """Handle scan timer timeout"""
        self.file_monitor.scan_directories()
        self.next_scan_time = time.time() + self.prefs['scan_interval_minutes'] * 60
    
    def update_countdown(self):
        """Update the countdown display"""
        remaining = self.next_scan_time - time.time()
        if remaining < 0:
            remaining = 0
        
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        self.countdown_label.setText(f"Next scan in: {minutes:02d}:{seconds:02d}")
    
    def scan_now(self):
        """Trigger an immediate scan"""
        self.logger.info("Manual scan triggered")
        self.file_monitor.scan_directories()
        self.next_scan_time = time.time() + self.prefs['scan_interval_minutes'] * 60
        self.update_countdown()
    
    def dragEnterEvent(self, event):
        """Accept drag events containing local .txrm / .txm files."""
        if event.mimeData().hasUrls():
            extensions = self._get_file_extensions()
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    ext = os.path.splitext(url.toLocalFile())[1].lower()
                    if ext in extensions:
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dropEvent(self, event):
        """Handle dropped .txrm / .txm files and process them immediately."""
        if event.mimeData().hasUrls():
            extensions = self._get_file_extensions()
            processed_any = False
            for url in event.mimeData().urls():
                if not url.isLocalFile():
                    continue
                filepath = os.path.normpath(url.toLocalFile())
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in extensions:
                    continue
                if not os.path.isfile(filepath):
                    self.logger.warning(f"Dropped path is not a file: {filepath}")
                    continue
                self.file_monitor.process_dropped_file(filepath)
                processed_any = True
            if processed_any:
                event.acceptProposedAction()
            else:
                event.ignore()

    def process_selected_now(self):
        """Process the currently selected file immediately"""
        selected_rows = self.file_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select a file to process.")
            return
        
        row = selected_rows[0].row()
        filepath_item = self.file_table.item(row, 0)
        if not filepath_item:
            return
        
        filepath = filepath_item.text()
        success = self.file_monitor.process_file_now(filepath)
        
        if not success:
            QMessageBox.warning(self, "Cannot Process", 
                              "This file cannot be processed now (already processing or completed).")
        else:
            self.logger.info(f"User requested immediate processing of: {filepath}")
    
    def _load_config_early(self):
        """Load configuration before logging is set up; silently uses defaults on error."""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                for key in DEFAULT_PREFS:
                    if key in config:
                        self.prefs[key] = config[key]
        except Exception:
            pass  # Use defaults silently.

    def show_preferences(self):
        """Open the Preferences dialog."""
        dialog = PreferencesDialog(self.prefs, self)
        if dialog.exec() == QDialog.Accepted:
            new_prefs = dialog.get_prefs()
            # Directories are managed via the main window buttons, not the dialog.
            new_prefs['directories'] = self.prefs['directories']
            self.prefs.update(new_prefs)
            self._apply_preferences()
            self.save_config()

    def _get_file_extensions(self) -> set:
        """Return the set of file extensions to monitor based on preferences."""
        extensions = set()
        if self.prefs.get('scan_txrm', True):
            extensions.add('.txrm')
        if self.prefs.get('scan_txm', False):
            extensions.add('.txm')
        return extensions or {'.txrm'}  # Default to .txrm if nothing is checked.

    def _apply_preferences(self):
        """Apply updated preferences to the running application."""
        self.file_monitor.set_file_extensions(self._get_file_extensions())
        self.file_monitor.set_output_fields(self.prefs['output_fields'])
        self.file_monitor.set_stability_seconds(self.prefs['stability_minutes'] * 60)

        scan_interval_ms = self.prefs['scan_interval_minutes'] * 60 * 1000
        self.scan_timer.stop()
        self.scan_timer.start(scan_interval_ms)
        self.next_scan_time = time.time() + (scan_interval_ms / 1000)

        self._update_log_handler(self.prefs['log_dir'])
        self.logger.info("Preferences updated")

    def _update_log_handler(self, new_log_dir: str):
        """Swap the rotating file handler to write to a new log directory."""
        for handler in list(self.logger.handlers):
            if isinstance(handler, RotatingFileHandler):
                handler.file_handler.close()
                self.logger.removeHandler(handler)
        Path(new_log_dir).mkdir(exist_ok=True, parents=True)
        file_handler = RotatingFileHandler(new_log_dir)
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
    
    def save_config(self):
        """Save current preferences to JSON file."""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.prefs, f, indent=2)
            self.logger.info(f"Configuration saved to {CONFIG_FILE}")
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")
    
    def closeEvent(self, event):
        """Handle window close event"""
        self.logger.info("TXRM Monitor application closing")
        self.save_config()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = TXRMMonitorApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
