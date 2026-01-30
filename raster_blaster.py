# -*- coding: utf-8 -*-
"""
Raster Blaster - Improved Version
A QGIS plugin for streamlined raster georeferencing using GDAL.

Quick Wins Implemented:
1. Background processing with QgsTask (no more UI freezes)
2. Progress bar support
3. User-selectable CRS (not hardcoded to EPSG:3857)
4. Persistent settings (remembers your preferences)
5. Auto-load results into QGIS
"""

import os
import subprocess
import csv
import tempfile
import time
import re

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QFileDialog, QMessageBox,
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QLineEdit,
    QToolBar, QComboBox, QCheckBox, QProgressBar, QGroupBox, QSpinBox
)
from qgis.core import (
    QgsMessageLog, Qgis, QgsTask, QgsApplication, 
    QgsRasterLayer, QgsProject, QgsSettings,
    QgsCoordinateReferenceSystem
)
from qgis.gui import QgsProjectionSelectionWidget

# Qt5/Qt6 compatibility for QMessageBox button enums
try:
    # Qt6 style
    QMessageBoxYes = QMessageBox.StandardButton.Yes
    QMessageBoxNo = QMessageBox.StandardButton.No
except AttributeError:
    # Qt5 style
    QMessageBoxYes = QMessageBox.Yes
    QMessageBoxNo = QMessageBox.No


class GdalTask(QgsTask):
    """
    Background task for running GDAL commands without freezing the UI.
    Parses GDAL progress output to update the task progress bar.
    """
    
    def __init__(self, description, commands, cleanup_files=None, output_file=None):
        """
        Args:
            description: Task description shown in task manager
            commands: List of (cmd_list, cmd_description) tuples to execute
            cleanup_files: List of temp files to delete after completion
            output_file: Path to output file (for auto-loading)
        """
        super().__init__(description, QgsTask.CanCancel)
        self.commands = commands
        self.cleanup_files = cleanup_files or []
        self.output_file = output_file
        self.error_message = None
        self.elapsed_time = 0
        self.exception = None
    
    def run(self):
        """Execute GDAL commands in background thread."""
        start_time = time.time()
        total_commands = len(self.commands)
        
        try:
            for idx, (cmd, cmd_desc) in enumerate(self.commands):
                if self.isCanceled():
                    return False
                
                # Base progress for this command
                base_progress = (idx / total_commands) * 100
                command_weight = 100 / total_commands
                
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Running {cmd_desc}',
                    'Raster Blaster', level=Qgis.Info
                )
                QgsMessageLog.logMessage(
                    f'Command: {" ".join(cmd)}',
                    'Raster Blaster', level=Qgis.Info
                )
                
                # Run process and capture progress
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1
                )
                
                # Read stderr for progress updates (GDAL outputs progress there)
                stderr_output = []
                while True:
                    if self.isCanceled():
                        process.terminate()
                        return False
                    
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break
                    
                    if line:
                        stderr_output.append(line)
                        # Parse GDAL progress (format: "...10...20...30..." or percentage)
                        progress_match = re.search(r'(\d+)(?:\.\d+)?%?\.{0,3}$', line.strip())
                        if progress_match:
                            try:
                                pct = float(progress_match.group(1))
                                overall = base_progress + (pct / 100) * command_weight
                                self.setProgress(overall)
                            except ValueError:
                                pass
                
                # Get remaining output
                _, remaining_stderr = process.communicate()
                stderr_output.append(remaining_stderr)
                
                if process.returncode != 0:
                    self.error_message = ''.join(stderr_output).strip()
                    return False
                
                # Update progress for completed command
                self.setProgress(base_progress + command_weight)
            
            self.elapsed_time = time.time() - start_time
            return True
            
        except Exception as e:
            self.exception = e
            self.error_message = str(e)
            return False
    
    def finished(self, result):
        """Called when task completes (in main thread)."""
        # Clean up temp files
        for f in self.cleanup_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
    
    def cancel(self):
        """Handle task cancellation."""
        QgsMessageLog.logMessage(
            'Raster Blaster: Task cancelled by user',
            'Raster Blaster', level=Qgis.Warning
        )
        super().cancel()


class raster_blaster:
    """Main plugin class."""
    
    # Settings keys
    SETTINGS_PREFIX = 'raster_blaster/'
    SETTING_LAST_DIR = 'last_directory'
    SETTING_COMPRESSION = 'compression'
    SETTING_RESAMPLING = 'resampling'
    SETTING_TRANSFORM = 'transformation'
    SETTING_CRS = 'target_crs'
    SETTING_AUTO_LOAD = 'auto_load_result'
    SETTING_JPEG_QUALITY = 'jpeg_quality'
    
    def __init__(self, iface):
        self.iface = iface
        self.connected = False
        self.gcp_table = None
        self.settings = QgsSettings()
        self.active_tasks = []

        # Kick off polling for the Georeferencer window
        QTimer.singleShot(1000, self.try_connect)

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        # Points→GeoTIFF
        self.act_full = QAction(icon, "Points→GeoTIFF", self.iface.mainWindow())
        self.act_full.triggered.connect(self.full_process_dialog)
        self.iface.addPluginToRasterMenu("&Raster Blaster", self.act_full)

        # Points→COG
        self.act_to_cog = QAction(icon, "Points→COG", self.iface.mainWindow())
        self.act_to_cog.triggered.connect(self.full_to_cog_dialog)
        self.iface.addPluginToRasterMenu("&Raster Blaster", self.act_to_cog)

        # GeoTIFF→COG
        self.act_cog = QAction(icon, "GeoTIFF→COG", self.iface.mainWindow())
        self.act_cog.triggered.connect(self.gdal_cog_dialog)
        self.iface.addPluginToRasterMenu("&Raster Blaster", self.act_cog)
        
        # Create main QGIS toolbar
        self.toolbar = self.iface.addToolBar("Raster Blaster")
        self.toolbar.setObjectName("RasterBlasterToolbar")
        
        # Create separate actions for toolbar (so they can have different text/tooltips)
        self.tb_act_full = QAction(icon, "Points→GeoTIFF", self.iface.mainWindow())
        self.tb_act_full.setToolTip("Convert points file + image to GeoTIFF")
        self.tb_act_full.triggered.connect(self.full_process_dialog)
        self.toolbar.addAction(self.tb_act_full)
        
        self.tb_act_to_cog = QAction(icon, "Points→COG", self.iface.mainWindow())
        self.tb_act_to_cog.setToolTip("Convert points file + image to Cloud-Optimized GeoTIFF")
        self.tb_act_to_cog.triggered.connect(self.full_to_cog_dialog)
        self.toolbar.addAction(self.tb_act_to_cog)
        
        self.tb_act_cog = QAction(icon, "GeoTIFF→COG", self.iface.mainWindow())
        self.tb_act_cog.setToolTip("Convert GeoTIFF to Cloud-Optimized GeoTIFF")
        self.tb_act_cog.triggered.connect(self.gdal_cog_dialog)
        self.toolbar.addAction(self.tb_act_cog)

    def unload(self):
        # Remove menu items
        for act in (self.act_full, self.act_to_cog, self.act_cog):
            try:
                self.iface.removePluginRasterMenu("&Raster Blaster", act)
            except Exception:
                pass
        
        # Remove toolbar
        if hasattr(self, 'toolbar') and self.toolbar:
            try:
                del self.toolbar
            except Exception:
                pass

    def try_connect(self):
        """Poll for Georeferencer window and add toolbar buttons when found."""
        for w in QApplication.topLevelWidgets():
            if w.metaObject().className() == 'QgsGeoreferencerMainWindow':
                QgsMessageLog.logMessage(
                    'Raster Blaster: Found Georeferencer', 
                    'Raster Blaster', level=Qgis.Info
                )
                self.setup_georef(w)
                return
        QTimer.singleShot(1000, self.try_connect)

    def setup_georef(self, georef):
        """Add buttons to Georeferencer toolbar."""
        tb = georef.findChild(QToolBar, 'toolBarFile')
        if not tb:
            return
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        
        for txt, cb in [
            ('Points→GeoTIFF', self.full_process_dialog),
            ('Points→COG', self.full_to_cog_dialog),
            ('GeoTIFF→COG', self.gdal_cog_dialog)
        ]:
            act = QAction(icon, txt, georef)
            act.triggered.connect(cb)
            tb.addSeparator()
            tb.addAction(act)

    # =========================================================================
    # Settings helpers
    # =========================================================================
    
    def get_setting(self, key, default=''):
        """Retrieve a saved setting."""
        return self.settings.value(self.SETTINGS_PREFIX + key, default)
    
    def save_setting(self, key, value):
        """Save a setting."""
        self.settings.setValue(self.SETTINGS_PREFIX + key, value)

    # =========================================================================
    # Georeferencer detection
    # =========================================================================
    
    def get_georeferencer_info(self):
        """
        Get information from the Georeferencer if it's open.
        Optimized for speed - prioritizes fast detection methods.
        
        Returns dict with:
            'input_file': path to the raster being georeferenced (or None)
            'points_file': path to the .points file (or None)
            'georeferencer_open': True if Georeferencer window is found
        """
        result = {
            'input_file': None,
            'points_file': None,
            'georeferencer_open': False
        }
        
        # Find the Georeferencer window
        georef_window = None
        for w in QApplication.topLevelWidgets():
            class_name = w.metaObject().className()
            if class_name == 'QgsGeoreferencerMainWindow':
                georef_window = w
                result['georeferencer_open'] = True
                break
        
        if not georef_window:
            return result
        
        filename_from_title = None
        
        # Method 1: Parse window title (FAST)
        try:
            window_title = georef_window.windowTitle()
            if ' - ' in window_title:
                potential_path = window_title.split(' - ', 1)[-1].strip()
                if potential_path:
                    if os.path.exists(potential_path):
                        result['input_file'] = potential_path
                    else:
                        filename_from_title = potential_path
        except Exception:
            pass
        
        # Method 2: Check status bar current message (FAST)
        if not result['input_file']:
            try:
                from qgis.PyQt.QtWidgets import QStatusBar
                status_bar = georef_window.findChild(QStatusBar)
                if status_bar:
                    current_msg = status_bar.currentMessage()
                    if current_msg and ':' in current_msg:
                        potential_path = current_msg.split(':', 1)[-1].strip()
                        if potential_path and os.path.exists(potential_path):
                            result['input_file'] = potential_path
            except Exception:
                pass
        
        # Method 3: Check map canvas layers (FAST)
        if not result['input_file']:
            try:
                from qgis.gui import QgsMapCanvas
                canvases = georef_window.findChildren(QgsMapCanvas)
                for canvas in canvases:
                    layers = canvas.layers()
                    for layer in layers:
                        if layer and hasattr(layer, 'source'):
                            source = layer.source()
                            if source and os.path.exists(source):
                                result['input_file'] = source
                                break
                    if result['input_file']:
                        break
            except Exception:
                pass
        
        # Method 4: Quick check georeferencer settings (FAST - only georef keys)
        if not result['input_file'] and filename_from_title:
            try:
                settings = QgsSettings()
                # Only scan keys containing 'georef' - much faster than all keys
                all_keys = settings.allKeys()
                georef_keys = [k for k in all_keys if 'georef' in k.lower()]
                
                for key in georef_keys:
                    val = settings.value(key, '')
                    if val and isinstance(val, str):
                        # Check if this is a file path ending with our filename
                        if val.endswith(filename_from_title) and os.path.exists(val):
                            result['input_file'] = val
                            break
                        # Check if this is a directory containing our file
                        if os.path.isdir(val):
                            potential_path = os.path.join(val, filename_from_title)
                            if os.path.exists(potential_path):
                                result['input_file'] = potential_path
                                break
            except Exception:
                pass
        
        # Method 5: Check our plugin's last used directory (FAST)
        if not result['input_file'] and filename_from_title:
            try:
                last_dir = self.get_setting(self.SETTING_LAST_DIR, '')
                if last_dir and os.path.isdir(last_dir):
                    potential_path = os.path.join(last_dir, filename_from_title)
                    if os.path.exists(potential_path):
                        result['input_file'] = potential_path
            except Exception:
                pass
        
        # Find matching .points file if we found the input file
        if result['input_file']:
            input_dir = os.path.dirname(result['input_file'])
            input_basename = os.path.splitext(os.path.basename(result['input_file']))[0]
            
            # First try exact match
            exact_points = os.path.splitext(result['input_file'])[0] + '.points'
            if os.path.exists(exact_points):
                result['points_file'] = exact_points
            else:
                # Search folder for .points files
                try:
                    points_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.points')]
                    if len(points_files) == 1:
                        result['points_file'] = os.path.join(input_dir, points_files[0])
                    elif len(points_files) > 1:
                        # Find best match
                        for pf in points_files:
                            pf_base = os.path.splitext(pf)[0].lower()
                            if input_basename.lower() in pf_base or pf_base in input_basename.lower():
                                result['points_file'] = os.path.join(input_dir, pf)
                                break
                        if not result['points_file']:
                            # Use most recent
                            points_paths = [os.path.join(input_dir, f) for f in points_files]
                            result['points_file'] = max(points_paths, key=os.path.getmtime)
                except Exception:
                    pass
        
        return result

    # =========================================================================
    # Dialog builder
    # =========================================================================
    
    def _gdal_dialog(self, title, fields, callback, initial_values=None):
        """
        Build a dialog with file selectors, dropdowns, and options.
        
        Fields format: list of (label, key, field_type)
        Field types:
            - 'points_file': .points file selector
            - 'input_file': generic input file selector
            - 'output_geotiff': output GeoTIFF selector
            - 'output_cog': output COG selector
            - 'transform': transformation method dropdown
            - 'resample': resampling method dropdown
            - 'compress': compression dropdown
            - 'crs': CRS selector widget
            - 'jpeg_quality': JPEG quality spinbox
        
        initial_values: optional dict of {key: value} for pre-filling fields
        """
        if initial_values is None:
            initial_values = {}
        
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(500)
        layout = QVBoxLayout()
        inputs = {}
        
        # File inputs group
        file_group = QGroupBox("Files")
        file_layout = QVBoxLayout()
        
        # Options group
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()
        
        for label, key, field_type in fields:
            hl = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setMinimumWidth(120)
            
            # CRS selector
            if field_type == 'crs':
                crs_widget = QgsProjectionSelectionWidget()
                # Load saved CRS or default to EPSG:3857
                saved_crs = self.get_setting(self.SETTING_CRS, 'EPSG:3857')
                crs_widget.setCrs(QgsCoordinateReferenceSystem(saved_crs))
                hl.addWidget(lbl)
                hl.addWidget(crs_widget)
                options_layout.addLayout(hl)
                inputs[key] = crs_widget
                continue
            
            # Transformation dropdown
            if field_type == 'transform':
                combo = QComboBox()
                combo.addItems([
                    'TPS',
                    'RPC',
                    'Geoloc',
                    'Polynomial (order 1)',
                    'Polynomial (order 2)',
                    'Polynomial (order 3)'
                ])
                saved = self.get_setting(self.SETTING_TRANSFORM, 'TPS')
                idx = combo.findText(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                hl.addWidget(lbl)
                hl.addWidget(combo)
                options_layout.addLayout(hl)
                inputs[key] = combo
                continue

            # Resampling dropdown
            if field_type == 'resample':
                combo = QComboBox()
                combo.addItems([
                    'lanczos', 'near', 'bilinear', 'cubic', 'cubicspline',
                    'average', 'mode', 'max', 'min', 'med'
                ])
                saved = self.get_setting(self.SETTING_RESAMPLING, 'lanczos')
                idx = combo.findText(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                hl.addWidget(lbl)
                hl.addWidget(combo)
                options_layout.addLayout(hl)
                inputs[key] = combo
                continue

            # Compression dropdown
            if field_type == 'compress':
                combo = QComboBox()
                combo.addItems(['JPEG', 'LZW', 'DEFLATE', 'PACKBITS', 'ZSTD', 'NONE'])
                saved = self.get_setting(self.SETTING_COMPRESSION, 'JPEG')
                idx = combo.findText(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                hl.addWidget(lbl)
                hl.addWidget(combo)
                options_layout.addLayout(hl)
                inputs[key] = combo
                continue
            
            # JPEG Quality spinbox
            if field_type == 'jpeg_quality':
                spin = QSpinBox()
                spin.setRange(1, 100)
                spin.setValue(int(self.get_setting(self.SETTING_JPEG_QUALITY, '85')))
                spin.setSuffix('%')
                hl.addWidget(lbl)
                hl.addWidget(spin)
                options_layout.addLayout(hl)
                inputs[key] = spin
                continue

            # File selectors
            edit = QLineEdit()
            btn = QPushButton('Browse...')
            
            if field_type == 'points_file':
                def make_browse_points(edit_widget):
                    def browse_points():
                        path, _ = QFileDialog.getOpenFileName(
                            None, "Select Points File",
                            "",
                            "Points Files (*.points);;All Files (*)"
                        )
                        if path:
                            edit_widget.setText(path)
                    return browse_points
                btn.clicked.connect(make_browse_points(edit))
                hl.addWidget(lbl)
                hl.addWidget(edit)
                hl.addWidget(btn)
                file_layout.addLayout(hl)
                
            elif field_type == 'input_file':
                def make_browse_input(edit_widget, all_inputs, all_fields):
                    def browse_input():
                        path, _ = QFileDialog.getOpenFileName(
                            None, "Select Input Image",
                            "",
                            "Image Files (*.tif *.tiff *.jpg *.jpeg *.png);;All Files (*)"
                        )
                        if path:
                            edit_widget.setText(path)
                            
                            # Auto-fill output field if empty
                            for flabel, fkey, ftype in all_fields:
                                if ftype in ('output_geotiff', 'output_cog'):
                                    output_widget = all_inputs.get(fkey)
                                    if output_widget and not output_widget.text():
                                        suffix = '_cog.tif' if 'cog' in ftype else '_georef.tif'
                                        output_path = os.path.splitext(path)[0] + suffix
                                        output_widget.setText(output_path)
                                    break
                    return browse_input
                btn.clicked.connect(make_browse_input(edit, inputs, fields))
                hl.addWidget(lbl)
                hl.addWidget(edit)
                hl.addWidget(btn)
                file_layout.addLayout(hl)
                
            elif field_type in ('output_geotiff', 'output_cog'):
                suffix = '_cog.tif' if 'cog' in field_type else '_georef.tif'
                
                def make_browse_output(edit_widget, sfx):
                    def browse_output():
                        # Try to suggest name based on input
                        inp = inputs.get('input_file')
                        base = ''
                        if inp and inp.text():
                            base = os.path.splitext(inp.text())[0] + sfx
                        path, _ = QFileDialog.getSaveFileName(
                            None, "Save Output",
                            base,
                            "TIFF Files (*.tif)"
                        )
                        if path:
                            if not path.lower().endswith('.tif'):
                                path += '.tif'
                            edit_widget.setText(path)
                    return browse_output
                btn.clicked.connect(make_browse_output(edit, suffix))
                hl.addWidget(lbl)
                hl.addWidget(edit)
                hl.addWidget(btn)
                file_layout.addLayout(hl)
            
            inputs[key] = edit
            
            # Pre-fill from initial_values if provided
            if key in initial_values and initial_values[key]:
                edit.setText(initial_values[key])
        
        # Add groups to main layout
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)
        
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)
        
        # Auto-load checkbox
        auto_load_cb = QCheckBox("Automatically add result to map")
        auto_load_cb.setChecked(self.get_setting(self.SETTING_AUTO_LOAD, 'true') == 'true')
        layout.addWidget(auto_load_cb)
        inputs['auto_load'] = auto_load_cb
        
        # Progress bar (hidden initially)
        progress = QProgressBar()
        progress.setVisible(False)
        progress.setTextVisible(True)
        layout.addWidget(progress)
        inputs['progress'] = progress
        
        # Status label
        status_label = QLabel("")
        layout.addWidget(status_label)
        inputs['status'] = status_label
        
        # Buttons
        btn_layout = QHBoxLayout()
        run_btn = QPushButton('Run')
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(dlg.reject)
        
        def on_run():
            # Save settings
            if 'compress' in inputs:
                self.save_setting(self.SETTING_COMPRESSION, inputs['compress'].currentText())
            if 'resample' in inputs:
                self.save_setting(self.SETTING_RESAMPLING, inputs['resample'].currentText())
            if 'transform' in inputs:
                self.save_setting(self.SETTING_TRANSFORM, inputs['transform'].currentText())
            if 'crs' in inputs:
                self.save_setting(self.SETTING_CRS, inputs['crs'].crs().authid())
            if 'jpeg_quality' in inputs:
                self.save_setting(self.SETTING_JPEG_QUALITY, str(inputs['jpeg_quality'].value()))
            self.save_setting(self.SETTING_AUTO_LOAD, 'true' if auto_load_cb.isChecked() else 'false')
            
            # Collect values
            values = {}
            for lbl, key, ftype in fields:
                widget = inputs[key]
                if isinstance(widget, QComboBox):
                    values[key] = widget.currentText()
                elif isinstance(widget, QgsProjectionSelectionWidget):
                    values[key] = widget.crs()
                elif isinstance(widget, QSpinBox):
                    values[key] = widget.value()
                else:
                    values[key] = widget.text()
            
            values['auto_load'] = auto_load_cb.isChecked()
            values['progress'] = progress
            values['status'] = status_label
            values['dialog'] = dlg
            values['run_button'] = run_btn
            
            callback(values)
        
        run_btn.clicked.connect(on_run)
        btn_layout.addWidget(run_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        
        dlg.setLayout(layout)
        
        # Qt5/Qt6 compatibility: exec_() renamed to exec() in Qt6
        if hasattr(dlg, 'exec'):
            dlg.exec()
        else:
            dlg.exec_()

    # =========================================================================
    # GeoTIFF → COG
    # =========================================================================
    
    def gdal_cog_dialog(self):
        self._gdal_dialog('GeoTIFF → COG', [
            ('Input GeoTIFF', 'input_file', 'input_file'),
            ('Compression', 'compress', 'compress'),
            ('JPEG Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output COG', 'output_file', 'output_cog')
        ], self.gdal_cog)

    def gdal_cog(self, values):
        """Convert GeoTIFF to COG using background task."""
        tif = values['input_file']
        compress = values['compress']
        jpeg_quality = values['jpeg_quality']
        cog = values['output_file']
        auto_load = values['auto_load']
        progress = values['progress']
        status = values['status']
        dlg = values['dialog']
        run_btn = values['run_button']
        
        # Validate inputs
        if not tif or not os.path.exists(tif):
            QMessageBox.warning(dlg, "Error", "Please select a valid input file.")
            return
        if not cog:
            QMessageBox.warning(dlg, "Error", "Please specify an output file.")
            return
        
        # Check if output file exists
        if os.path.exists(cog):
            reply = QMessageBox.question(
                dlg, "File Exists",
                f"Output file already exists:\n{os.path.basename(cog)}\n\nOverwrite?",
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
            # Delete existing file
            try:
                os.remove(cog)
            except Exception as e:
                QMessageBox.critical(dlg, "Error", f"Cannot delete existing file:\n{e}")
                return
        
        # Build command
        cmd = [
            'gdal_translate', tif, cog,
            '-of', 'COG',
            '-co', f'COMPRESS={compress}',
            '--config', 'GDAL_NUM_THREADS', 'ALL_CPUS'
        ]
        
        # Add JPEG quality if using JPEG compression
        if compress == 'JPEG':
            cmd.extend(['-co', f'QUALITY={jpeg_quality}'])
        
        # Show progress
        progress.setVisible(True)
        progress.setValue(0)
        status.setText("Processing...")
        run_btn.setEnabled(False)
        
        # Create and run task
        task = GdalTask(
            'Raster Blaster: Creating COG',
            [(cmd, 'gdal_translate → COG')],
            output_file=cog
        )
        
        def on_complete(exception, result=None):
            progress.setVisible(False)
            run_btn.setEnabled(True)
            
            if task.error_message:
                status.setText(f"Error: {task.error_message[:100]}")
                QgsMessageLog.logMessage(
                    f'Raster Blaster: COG failed: {task.error_message}',
                    'Raster Blaster', level=Qgis.Critical
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f"COG creation failed",
                    level=Qgis.Critical
                )
            else:
                mins, secs = divmod(int(task.elapsed_time), 60)
                status.setText(f"Complete! ({mins:02d}:{secs:02d})")
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f"COG created: {os.path.basename(cog)} ({mins:02d}:{secs:02d})",
                    level=Qgis.Success
                )
                
                if auto_load:
                    self.load_raster_layer(cog)
                
                dlg.accept()
        
        task.taskCompleted.connect(lambda: on_complete(None))
        task.taskTerminated.connect(lambda: on_complete(task.exception))
        
        QgsApplication.taskManager().addTask(task)
        self.active_tasks.append(task)

    # =========================================================================
    # Points → GeoTIFF
    # =========================================================================
    
    def full_process_dialog(self):
        # Try to get info from the Georeferencer if it's open
        georef_info = self.get_georeferencer_info()
        
        # Build initial values dict
        initial_values = {}
        if georef_info['input_file']:
            initial_values['input_file'] = georef_info['input_file']
            # Auto-generate output path with _georef suffix
            initial_values['output_file'] = os.path.splitext(georef_info['input_file'])[0] + '_georef.tif'
        if georef_info['points_file']:
            initial_values['points_file'] = georef_info['points_file']
        
        self._gdal_dialog('Points → GeoTIFF', [
            ('Points File', 'points_file', 'points_file'),
            ('Input Image', 'input_file', 'input_file'),
            ('Target CRS', 'crs', 'crs'),
            ('Transformation', 'transform', 'transform'),
            ('Resampling', 'resample', 'resample'),
            ('Compression', 'compress', 'compress'),
            ('JPEG Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output GeoTIFF', 'output_file', 'output_geotiff')
        ], self.full_process, initial_values)

    def full_process(self, values):
        """Create GeoTIFF from points file using background task."""
        pf = values['points_file']
        tif = values['input_file']
        crs = values['crs']
        transform = values['transform']
        resample = values['resample']
        compress = values['compress']
        jpeg_quality = values['jpeg_quality']
        out_tif = values['output_file']
        auto_load = values['auto_load']
        progress = values['progress']
        status = values['status']
        dlg = values['dialog']
        run_btn = values['run_button']
        
        # Validate inputs
        if not pf or not os.path.exists(pf):
            QMessageBox.warning(dlg, "Error", "Please select a valid points file.")
            return
        if not tif or not os.path.exists(tif):
            QMessageBox.warning(dlg, "Error", "Please select a valid input image.")
            return
        if not out_tif:
            QMessageBox.warning(dlg, "Error", "Please specify an output file.")
            return
        
        # Check if output file exists
        if os.path.exists(out_tif):
            reply = QMessageBox.question(
                dlg, "File Exists",
                f"Output file already exists:\n{os.path.basename(out_tif)}\n\nOverwrite?",
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
            try:
                os.remove(out_tif)
            except Exception as e:
                QMessageBox.critical(dlg, "Error", f"Cannot delete existing file:\n{e}")
                return
        
        # Parse GCPs from points file
        try:
            gcp_data = self.parse_points_file(pf)
            if gcp_data['count'] == 0:
                QMessageBox.warning(dlg, "Error", "No enabled GCPs found in points file.")
                return
        except Exception as e:
            QMessageBox.warning(dlg, "Error", f"Failed to read points file: {e}")
            return
        
        # Validate GCP count for selected transformation
        is_valid, warning_msg = self.validate_gcps_for_transform(gcp_data['count'], transform)
        if not is_valid:
            QMessageBox.critical(dlg, "Insufficient GCPs", warning_msg)
            return
        if warning_msg:
            reply = QMessageBox.warning(
                dlg, "GCP Warning", warning_msg,
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
        
        # Check GCP distribution
        distribution_warning = self.check_gcp_distribution(gcp_data['gcps'])
        if distribution_warning:
            reply = QMessageBox.warning(
                dlg, "GCP Distribution Warning", distribution_warning,
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
        
        # Create temp VRT path
        tmp_vrt = tempfile.NamedTemporaryFile(delete=False, suffix='.vrt')
        tmp_vrt.close()
        vrt_path = tmp_vrt.name
        
        # Build gdal_translate command (create VRT with GCPs)
        cmd1 = ['gdal_translate', '-of', 'VRT'] + gcp_data['args'] + [tif, vrt_path]
        
        # Build gdalwarp command
        if transform.lower().startswith('polynomial'):
            order = transform.split('order')[-1].strip().strip(')')
            transform_args = ['-order', order]
        else:
            transform_args = [f'-{transform.lower()}']
        
        cmd2 = [
            'gdalwarp',
            '-t_srs', crs.authid(),
            '-r', resample,
            *transform_args,
            '--config', 'GDAL_NUM_THREADS', 'ALL_CPUS',
            '-wo', 'NUM_THREADS=ALL_CPUS',
            '-multi',
            '-co', 'BIGTIFF=YES',
            '-co', 'TILED=YES',
            '-co', f'COMPRESS={compress}'
        ]
        
        if compress == 'JPEG':
            cmd2.extend(['-co', f'JPEG_QUALITY={jpeg_quality}'])
        
        cmd2.extend([vrt_path, out_tif])
        
        # Show progress
        progress.setVisible(True)
        progress.setValue(0)
        status.setText("Processing...")
        run_btn.setEnabled(False)
        
        # Create task
        task = GdalTask(
            'Raster Blaster: Creating GeoTIFF',
            [
                (cmd1, 'gdal_translate → VRT'),
                (cmd2, 'gdalwarp → GeoTIFF')
            ],
            cleanup_files=[vrt_path],
            output_file=out_tif
        )
        
        def on_complete(exception, result=None):
            progress.setVisible(False)
            run_btn.setEnabled(True)
            
            if task.error_message:
                status.setText(f"Error: {task.error_message[:100]}")
                QgsMessageLog.logMessage(
                    f'Raster Blaster: GeoTIFF failed: {task.error_message}',
                    'Raster Blaster', level=Qgis.Critical
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    "GeoTIFF creation failed",
                    level=Qgis.Critical
                )
            else:
                mins, secs = divmod(int(task.elapsed_time), 60)
                status.setText(f"Complete! ({mins:02d}:{secs:02d})")
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f"GeoTIFF created: {os.path.basename(out_tif)} ({mins:02d}:{secs:02d})",
                    level=Qgis.Success
                )
                
                if auto_load:
                    self.load_raster_layer(out_tif)
                
                dlg.accept()
        
        task.taskCompleted.connect(lambda: on_complete(None))
        task.taskTerminated.connect(lambda: on_complete(task.exception))
        
        QgsApplication.taskManager().addTask(task)
        self.active_tasks.append(task)

    # =========================================================================
    # Points → COG
    # =========================================================================
    
    def full_to_cog_dialog(self):
        # Try to get info from the Georeferencer if it's open
        georef_info = self.get_georeferencer_info()
        
        # Build initial values dict
        initial_values = {}
        if georef_info['input_file']:
            initial_values['input_file'] = georef_info['input_file']
            # Auto-generate output path with _cog suffix
            initial_values['output_file'] = os.path.splitext(georef_info['input_file'])[0] + '_cog.tif'
        if georef_info['points_file']:
            initial_values['points_file'] = georef_info['points_file']
        
        self._gdal_dialog('Points → COG', [
            ('Points File', 'points_file', 'points_file'),
            ('Input Image', 'input_file', 'input_file'),
            ('Target CRS', 'crs', 'crs'),
            ('Transformation', 'transform', 'transform'),
            ('Resampling', 'resample', 'resample'),
            ('Compression', 'compress', 'compress'),
            ('JPEG Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output COG', 'output_file', 'output_cog')
        ], self.full_to_cog, initial_values)

    def full_to_cog(self, values):
        """Create COG from points file using background task."""
        pf = values['points_file']
        tif = values['input_file']
        crs = values['crs']
        transform = values['transform']
        resample = values['resample']
        compress = values['compress']
        jpeg_quality = values['jpeg_quality']
        out_cog = values['output_file']
        auto_load = values['auto_load']
        progress = values['progress']
        status = values['status']
        dlg = values['dialog']
        run_btn = values['run_button']
        
        # Validate inputs
        if not pf or not os.path.exists(pf):
            QMessageBox.warning(dlg, "Error", "Please select a valid points file.")
            return
        if not tif or not os.path.exists(tif):
            QMessageBox.warning(dlg, "Error", "Please select a valid input image.")
            return
        if not out_cog:
            QMessageBox.warning(dlg, "Error", "Please specify an output file.")
            return
        
        # Check if output file exists
        if os.path.exists(out_cog):
            reply = QMessageBox.question(
                dlg, "File Exists",
                f"Output file already exists:\n{os.path.basename(out_cog)}\n\nOverwrite?",
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
            try:
                os.remove(out_cog)
            except Exception as e:
                QMessageBox.critical(dlg, "Error", f"Cannot delete existing file:\n{e}")
                return
        
        # Parse GCPs
        try:
            gcp_data = self.parse_points_file(pf)
            if gcp_data['count'] == 0:
                QMessageBox.warning(dlg, "Error", "No enabled GCPs found in points file.")
                return
        except Exception as e:
            QMessageBox.warning(dlg, "Error", f"Failed to read points file: {e}")
            return
        
        # Validate GCP count for selected transformation
        is_valid, warning_msg = self.validate_gcps_for_transform(gcp_data['count'], transform)
        if not is_valid:
            QMessageBox.critical(dlg, "Insufficient GCPs", warning_msg)
            return
        if warning_msg:
            reply = QMessageBox.warning(
                dlg, "GCP Warning", warning_msg,
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
        
        # Check GCP distribution
        distribution_warning = self.check_gcp_distribution(gcp_data['gcps'])
        if distribution_warning:
            reply = QMessageBox.warning(
                dlg, "GCP Distribution Warning", distribution_warning,
                QMessageBoxYes | QMessageBoxNo, QMessageBoxNo
            )
            if reply == QMessageBoxNo:
                return
        
        # Create temp VRT
        tmp_vrt = tempfile.NamedTemporaryFile(delete=False, suffix='.vrt')
        tmp_vrt.close()
        vrt_path = tmp_vrt.name
        
        # Build commands
        cmd1 = ['gdal_translate', '-of', 'VRT'] + gcp_data['args'] + [tif, vrt_path]
        
        if transform.lower().startswith('polynomial'):
            order = transform.split('order')[-1].strip().strip(')')
            transform_args = ['-order', order]
        else:
            transform_args = [f'-{transform.lower()}']
        
        cmd2 = [
            'gdalwarp',
            '-of', 'COG',
            '-t_srs', crs.authid(),
            '-r', resample,
            *transform_args,
            '--config', 'GDAL_NUM_THREADS', 'ALL_CPUS',
            '-wo', 'NUM_THREADS=ALL_CPUS',
            '-multi',
            '-co', 'BIGTIFF=YES',
            '-co', f'COMPRESS={compress}'
        ]
        
        if compress == 'JPEG':
            cmd2.extend(['-co', f'QUALITY={jpeg_quality}'])
        
        cmd2.extend([vrt_path, out_cog])
        
        # Show progress
        progress.setVisible(True)
        progress.setValue(0)
        status.setText("Processing...")
        run_btn.setEnabled(False)
        
        # Create task
        task = GdalTask(
            'Raster Blaster: Creating COG',
            [
                (cmd1, 'gdal_translate → VRT'),
                (cmd2, 'gdalwarp → COG')
            ],
            cleanup_files=[vrt_path],
            output_file=out_cog
        )
        
        def on_complete(exception, result=None):
            progress.setVisible(False)
            run_btn.setEnabled(True)
            
            if task.error_message:
                status.setText(f"Error: {task.error_message[:100]}")
                QgsMessageLog.logMessage(
                    f'Raster Blaster: COG failed: {task.error_message}',
                    'Raster Blaster', level=Qgis.Critical
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    "COG creation failed",
                    level=Qgis.Critical
                )
            else:
                mins, secs = divmod(int(task.elapsed_time), 60)
                status.setText(f"Complete! ({mins:02d}:{secs:02d})")
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f"COG created: {os.path.basename(out_cog)} ({mins:02d}:{secs:02d})",
                    level=Qgis.Success
                )
                
                if auto_load:
                    self.load_raster_layer(out_cog)
                
                dlg.accept()
        
        task.taskCompleted.connect(lambda: on_complete(None))
        task.taskTerminated.connect(lambda: on_complete(task.exception))
        
        QgsApplication.taskManager().addTask(task)
        self.active_tasks.append(task)

    # =========================================================================
    # Utility methods
    # =========================================================================
    
    def parse_points_file(self, filepath):
        """
        Parse a QGIS .points file and return GCP info.
        
        Returns dict with:
            'args': list like ['-gcp', 'sx', 'sy', 'mx', 'my', ...]
            'count': number of enabled GCPs
            'gcps': list of (sx, sy, mx, my) tuples for validation
        """
        result = {
            'args': [],
            'count': 0,
            'gcps': []
        }
        
        # Try different encodings
        encodings = ['utf-8', 'windows-1252', 'latin-1']
        content = None
        
        for encoding in encodings:
            try:
                with open(filepath, 'r', encoding=encoding) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue
        
        if content is None:
            raise ValueError("Could not decode points file with any supported encoding")
        
        # Filter out comments and empty lines
        lines = [l for l in content.splitlines() if l.strip() and not l.startswith('#')]
        
        if not lines:
            return result
        
        # Parse as CSV
        reader = csv.DictReader(lines)
        
        for row in reader:
            # Check if GCP is enabled
            enable = row.get('enable', '1').strip()
            if enable != '1':
                continue
            
            try:
                sx = float(row['sourceX'])
                sy = -float(row['sourceY'])  # QGIS uses inverted Y for source
                mx = float(row['mapX'])
                my = float(row['mapY'])
                result['args'].extend(['-gcp', str(sx), str(sy), str(mx), str(my)])
                result['gcps'].append((sx, sy, mx, my))
                result['count'] += 1
            except (KeyError, ValueError) as e:
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Skipping invalid GCP row: {e}',
                    'Raster Blaster', level=Qgis.Warning
                )
                continue
        
        return result
    
    def validate_gcps_for_transform(self, gcp_count, transform_type):
        """
        Validate that there are enough GCPs for the selected transformation.
        
        Returns tuple: (is_valid, warning_message or None)
        
        Minimum GCP requirements:
        - Polynomial order 1: 3 GCPs (affine)
        - Polynomial order 2: 6 GCPs
        - Polynomial order 3: 10 GCPs
        - TPS: 1 GCP minimum, but 10+ recommended
        - RPC/Geoloc: varies, typically need several
        """
        transform_lower = transform_type.lower()
        
        # Define minimum requirements
        if 'polynomial' in transform_lower:
            if 'order 1' in transform_lower or 'order1' in transform_lower:
                min_gcps = 3
                recommended = 4
                transform_name = "Polynomial (order 1)"
            elif 'order 2' in transform_lower or 'order2' in transform_lower:
                min_gcps = 6
                recommended = 8
                transform_name = "Polynomial (order 2)"
            elif 'order 3' in transform_lower or 'order3' in transform_lower:
                min_gcps = 10
                recommended = 15
                transform_name = "Polynomial (order 3)"
            else:
                min_gcps = 3
                recommended = 4
                transform_name = "Polynomial"
        elif transform_lower == 'tps':
            min_gcps = 1
            recommended = 10
            transform_name = "Thin Plate Spline (TPS)"
        else:
            # RPC, Geoloc - less strict
            min_gcps = 1
            recommended = 6
            transform_name = transform_type
        
        # Check requirements
        if gcp_count < min_gcps:
            return (False, 
                f"{transform_name} requires at least {min_gcps} GCPs.\n"
                f"You only have {gcp_count} enabled GCP(s).\n\n"
                f"Please add more ground control points or choose a different transformation method."
            )
        elif gcp_count < recommended:
            return (True,
                f"Warning: {transform_name} works best with {recommended}+ GCPs.\n"
                f"You have {gcp_count} GCP(s). Results may be less accurate.\n\n"
                f"Continue anyway?"
            )
        
        return (True, None)
    
    def check_gcp_distribution(self, gcps):
        """
        Check if GCPs are well-distributed across the image.
        
        Returns warning message if GCPs appear clustered, None otherwise.
        """
        if len(gcps) < 3:
            return None
        
        # Extract source coordinates
        src_x = [g[0] for g in gcps]
        src_y = [g[1] for g in gcps]
        
        # Calculate spread (using range as simple metric)
        x_range = max(src_x) - min(src_x)
        y_range = max(src_y) - min(src_y)
        
        # Calculate centroid
        cx = sum(src_x) / len(src_x)
        cy = sum(src_y) / len(src_y)
        
        # Check if all points are in one quadrant relative to centroid
        # (simple clustering detection)
        quadrants = set()
        for x, y in zip(src_x, src_y):
            q = (1 if x >= cx else 0, 1 if y >= cy else 0)
            quadrants.add(q)
        
        if len(quadrants) == 1 and len(gcps) >= 4:
            return (
                "Warning: All GCPs appear to be clustered in one area of the image.\n"
                "For best results, distribute GCPs across all corners and edges.\n\n"
                "Continue anyway?"
            )
        
        # Check for very narrow spread (points nearly collinear)
        if x_range > 0 and y_range > 0:
            aspect = min(x_range, y_range) / max(x_range, y_range)
            if aspect < 0.1 and len(gcps) >= 4:
                return (
                    "Warning: GCPs appear to be arranged in a nearly straight line.\n"
                    "This may cause distortion. Try adding GCPs that form a wider pattern.\n\n"
                    "Continue anyway?"
                )
        
        return None
    
    def load_raster_layer(self, filepath):
        """Load a raster file into QGIS as a new layer."""
        try:
            name = os.path.splitext(os.path.basename(filepath))[0]
            layer = QgsRasterLayer(filepath, name)
            
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Added layer "{name}" to map',
                    'Raster Blaster', level=Qgis.Info
                )
            else:
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Failed to load layer from {filepath}',
                    'Raster Blaster', level=Qgis.Warning
                )
        except Exception as e:
            QgsMessageLog.logMessage(
                f'Raster Blaster: Error loading layer: {e}',
                'Raster Blaster', level=Qgis.Warning
            )
