import os
import subprocess
import csv
import tempfile
import time

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QFileDialog, QMessageBox,
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QLineEdit,
    QToolBar, QComboBox
)
from qgis.core import QgsMessageLog, Qgis


class raster_blaster:
    def __init__(self, iface):
        self.iface = iface
        self.connected = False
        self.gcp_table = None

        # Kick off polling for the Geo-referencer window
        QTimer.singleShot(1000, self.try_connect)

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))

        # Points→GeoTIFF
        self.act_full = QAction("Points→GeoTIFF", self.iface.mainWindow())
        self.act_full.triggered.connect(self.full_process_dialog)
        self.iface.addPluginToMenu("Raster Blaster", self.act_full)

        # Points→COG
        self.act_to_cog = QAction("Points→COG", self.iface.mainWindow())
        self.act_to_cog.triggered.connect(self.full_to_cog_dialog)
        self.iface.addPluginToMenu("Raster Blaster", self.act_to_cog)

        # GeoTIFF→COG
        self.act_cog = QAction("GeoTIFF→COG", self.iface.mainWindow())
        self.act_cog.triggered.connect(self.gdal_cog_dialog)
        self.iface.addPluginToMenu("Raster Blaster", self.act_cog)

    def unload(self):
        for act in (self.act_full, self.act_to_cog, self.act_cog):
            try:
                self.iface.removePluginMenu("Raster Blaster", act)
            except Exception:
                pass

    def try_connect(self):
        for w in QApplication.topLevelWidgets():
            if w.metaObject().className() == 'QgsGeoreferencerMainWindow':
                QgsMessageLog.logMessage('Raster Blaster: Found Georeferencer', 'Raster Blaster')
                self.setup_georef(w)
                return
        QTimer.singleShot(1000, self.try_connect)

    def setup_georef(self, georef):
        tb = georef.findChild(QToolBar, 'toolBarFile')
        if not tb:
            return
        for txt, cb in [
            ('Points→GeoTIFF', self.full_process_dialog),
            ('Points→COG',     self.full_to_cog_dialog),
            ('GeoTIFF→COG',    self.gdal_cog_dialog)
        ]:
            act = QAction(txt, georef)
            act.triggered.connect(cb)
            tb.addSeparator()
            tb.addAction(act)

    def _gdal_dialog(self, title, fields, callback):
        """
        Generic dialog builder.
        fields = list of (label, key, is_out).
        Keys:
         - "points_file" → filter "*.points"
         - "transform"   → QComboBox with TPS, RPC, Geoloc, Polynomial 1/2/3
         - "resample"    → QComboBox with all GDAL resampling methods
         - "compress"    → QComboBox with LZW, JPEG, DEFLATE, PACKBITS
         - is_out=True   → “Save .tif” dialog, auto-suffix
         - else          → generic “all files” open dialog
        """
        dlg = QDialog()
        dlg.setWindowTitle(title)
        layout = QVBoxLayout()
        inputs = {}

        for label, key, is_out in fields:
            hl = QHBoxLayout()
            lbl = QLabel(label)

            # Transformation dropdown
            if key == 'transform':
                combo = QComboBox()
                combo.addItems([
                    'TPS',
                    'RPC',
                    'Geoloc',
                    'Polynomial (order 1)',
                    'Polynomial (order 2)',
                    'Polynomial (order 3)'
                ])
                hl.addWidget(lbl)
                hl.addWidget(combo)
                layout.addLayout(hl)
                inputs[key] = combo
                continue

            # Resampling dropdown
            if key == 'resample':
                combo = QComboBox()
                combo.addItems([
                    'lanczos', 'near', 'bilinear', 'cubic', 'cubicspline',
                    'average', 'mode', 'max', 'min', 'med'
                ])
                hl.addWidget(lbl)
                hl.addWidget(combo)
                layout.addLayout(hl)
                inputs[key] = combo
                continue

            # Compression dropdown
            if key == 'compress':
                combo = QComboBox()
                combo.addItems(['JPEG', 'LZW', 'DEFLATE', 'PACKBITS'])
                hl.addWidget(lbl)
                hl.addWidget(combo)
                layout.addLayout(hl)
                inputs[key] = combo
                continue

            # Otherwise, use a QLineEdit + “Browse”
            edit = QLineEdit()
            btn = QPushButton('Browse')

            if key == 'points_file':
                # Only show *.points
                btn.clicked.connect(
                    lambda _, e=edit: e.setText(
                        QFileDialog.getOpenFileName(
                            None,
                            "Select Points File",
                            "",
                            "Points Files (*.points)"
                        )[0]
                    )
                )

            elif is_out:
                # “Save .tif” with auto-suffix
                def _save(e, lbl=label, key=key):
                    inp = inputs.get('input_tif')
                    base = os.path.splitext(inp.text())[0] if inp and inp.text() else ''
                    # if the field‐key contains "cog", use "_cog.tif", otherwise "_geotiff.tif"
                    if "cog" in key:
                        suffix = "_cog.tif"
                    else:
                        suffix = "_geotiff.tif"
                    default = base + suffix
                    path, _ = QFileDialog.getSaveFileName(
                        None,
                        f"Save {lbl}",
                        default,
                        "TIFF Files (*.tif)"
                    )
                    e.setText(path)

                btn.clicked.connect(lambda _, e=edit: _save(e))

            else:
                # Generic “All files (*)” open dialog
                btn.clicked.connect(
                    lambda _, e=edit: e.setText(
                        QFileDialog.getOpenFileName(
                            None,
                            f"Select {label}",
                            "",
                            "All files (*)"
                        )[0]
                    )
                )

            hl.addWidget(lbl)
            hl.addWidget(edit)
            hl.addWidget(btn)
            layout.addLayout(hl)
            inputs[key] = edit

        run = QPushButton('Run')
        run.clicked.connect(
            lambda: callback(
                *(inputs[k].currentText() if isinstance(inputs[k], QComboBox)
                  else inputs[k].text()
                  for _, k, _ in fields),
                dlg
            )
        )
        layout.addWidget(run)
        dlg.setLayout(layout)
        dlg.exec_()

    def gdal_cog_dialog(self):
        # Include Compression dropdown
        self._gdal_dialog('GeoTIFF→COG', [
            ('Input TIFF', 'input_tif', False),
            ('Compression', 'compress',   False),
            ('Output COG',  'output_cog', True)
        ], self.gdal_cog)

    def gdal_cog(self, tif, compress, cog, dlg):
        try:
            start = time.time()
            cmd = [
                'gdal_translate', tif, cog,
                '-of', 'COG',
                '-co', f'COMPRESS={compress}'
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                err = result.stderr.strip()
                QgsMessageLog.logMessage(
                    f'Raster Blaster: gdal_translate→COG failed: {err}',
                    'Raster Blaster', level=2
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f'COG error: {err}',
                    level=Qgis.Critical
                )
                return

            dlg.accept()
            elapsed = time.time() - start
            mins, secs = divmod(int(elapsed), 60)
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"COG created at {cog}  (Elapsed {mins:02d}:{secs:02d})",
                level=Qgis.Info
            )

        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"COG Error: {e}",
                level=Qgis.Critical
            )

    def full_process_dialog(self):
        # Include Transformation, Resampling, Compression dropdowns
        self._gdal_dialog('Points→GeoTIFF', [
            ('Points File',    'points_file', False),
            ('Input Image',    'input_tif',   False),
            ('Transformation', 'transform',   False),
            ('Resampling',     'resample',    False),
            ('Compression',    'compress',    False),
            ('Output GeoTIFF', 'output_tif',  True)
        ], self.full_process)

    def full_process(self, pf, tif, transform, resample, compress, out_tif, dlg):
        start = time.time()
        try:
            # Build GCP arguments
            gcps = []
            with open(pf, newline='', encoding='windows-1252') as f:
                lines = [l for l in f if not l.startswith('#')]
                for row in csv.DictReader(lines):
                    if row['enable'].strip() == '1':
                        sx, sy = float(row['sourceX']), -float(row['sourceY'])
                        mx, my = float(row['mapX']), float(row['mapY'])
                        gcps += ["-gcp", str(sx), str(sy), str(mx), str(my)]

            # Create temporary VRT
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.vrt')
            tmp.close()
            vrt_path = tmp.name

            # 1) translate → VRT
            subprocess.run(
                ["gdal_translate", "-of", "VRT"] + gcps + [tif, vrt_path],
                check=True
            )

            # 2) warp → GeoTIFF with chosen options (no -progress)
            if transform.lower().startswith('polynomial'):
                # Extract order from “Polynomial (order N)”
                order = transform.split('order')[-1].strip().strip(')')
                transform_arg = f"-order {order}"
            else:
                transform_arg = f"-{transform.lower()}"

            cmd2 = [
                "gdalwarp",
                "-t_srs", "EPSG:3857",
                "-r", resample,
                *transform_arg.split(),
                "--config", "GDAL_WARP_USE_OPENCL", "NO",
                "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                "-wo", "NUM_THREADS=ALL_CPUS",
                "-multi",
                "-co", "BIGTIFF=YES",
                "-co", "TILED=YES",
                "-co", f"COMPRESS={compress}",
                vrt_path, out_tif
            ]

            result = subprocess.run(cmd2, capture_output=True, text=True)
            if result.returncode != 0:
                err = result.stderr.strip()
                QgsMessageLog.logMessage(
                    f'Raster Blaster: gdalwarp→GeoTIFF failed: {err}',
                    'Raster Blaster', level=2
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f'Warp error: {err}',
                    level=Qgis.Critical
                )
                return

            elapsed = time.time() - start
            mins, secs = divmod(int(elapsed), 60)

            os.remove(vrt_path)
            dlg.accept()
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"GeoTIFF created at {out_tif}  (Elapsed {mins:02d}:{secs:02d})",
                level=Qgis.Info
            )

        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"Points→GeoTIFF Error: {e}",
                level=Qgis.Critical
            )
            try:
                os.remove(vrt_path)
            except:
                pass

    def full_to_cog_dialog(self):
        # Include the same three dropdowns here
        self._gdal_dialog('Points→COG', [
            ('Points File',    'points_file', False),
            ('Input Image',    'input_tif',   False),
            ('Transformation', 'transform',   False),
            ('Resampling',     'resample',    False),
            ('Compression',    'compress',    False),
            ('Output COG',     'output_cog',  True)
        ], self.full_to_cog)

    def full_to_cog(self, pf, tif, transform, resample, compress, out_cog, dlg):
        start = time.time()
        try:
            # Build GCP arguments
            gcps = []
            with open(pf, newline='', encoding='windows-1252') as f:
                lines = [l for l in f if not l.startswith('#')]
                for row in csv.DictReader(lines):
                    if row['enable'].strip() == '1':
                        sx, sy = float(row['sourceX']), -float(row['sourceY'])
                        mx, my = float(row['mapX']), float(row['mapY'])
                        gcps += ["-gcp", str(sx), str(sy), str(mx), str(my)]

            # Create temporary VRT
            tmp_vrt = tempfile.NamedTemporaryFile(delete=False, suffix='.vrt')
            tmp_vrt.close()
            vrt_path = tmp_vrt.name

            # 1) translate → VRT
            subprocess.run(
                ["gdal_translate", "-of", "VRT"] + gcps + [tif, vrt_path],
                check=True
            )

            # 2) warp → COG with chosen options (no -progress)
            if transform.lower().startswith('polynomial'):
                order = transform.split('order')[-1].strip().strip(')')
                transform_arg = f"-order {order}"
            else:
                transform_arg = f"-{transform.lower()}"

            cmd = [
                "gdalwarp",
                "-of", "COG",
                "-co", f"COMPRESS={compress}",
                "-t_srs", "EPSG:3857",
                "-r", resample,
                *transform_arg.split(),
                "--config", "GDAL_WARP_USE_OPENCL", "NO",
                "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                "-wo", "NUM_THREADS=ALL_CPUS",
                "-multi",
                "-co", "BIGTIFF=YES",
                vrt_path,
                out_cog
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                err = result.stderr.strip()
                QgsMessageLog.logMessage(
                    f'Raster Blaster: gdalwarp→COG failed: {err}',
                    'Raster Blaster', level=2
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster",
                    f'Warp error: {err}',
                    level=Qgis.Critical
                )
                return

            elapsed = time.time() - start
            mins, secs = divmod(int(elapsed), 60)

            os.remove(vrt_path)
            dlg.accept()
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"COG created at {out_cog}  (Elapsed {mins:02d}:{secs:02d})",
                level=Qgis.Info
            )

        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"Points→COG Error: {e}",
                level=Qgis.Critical
            )
            try:
                os.remove(vrt_path)
            except:
                pass
