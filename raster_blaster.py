# -*- coding: utf-8 -*-
"""
Raster Blaster
A QGIS plugin for streamlined raster georeferencing using GDAL.

- Background processing with QgsTask (no UI freezes), real progress + cancel
- Drives GDAL through the osgeo.gdal Python API (no subprocess / PATH / temp
  .vrt dependencies, no command-line length limits on large GCP sets)
- Uses the whole machine: all CPUs and a RAM-scaled block cache by default,
  with a Performance panel to dial threads / cache down
- User-selectable CRS, persistent settings, auto-load results into QGIS
"""

import os
import csv
import time
from contextlib import contextmanager

from osgeo import gdal

from qgis.PyQt.QtCore import QTimer
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

# ---------------------------------------------------------------------------
# Qt5 / Qt6  +  QGIS 3 / QGIS 4 compatibility shims
# ---------------------------------------------------------------------------

# Enum access that works on both Qt6 / QGIS 4 (scoped enums are mandatory) and
# Qt5 / QGIS 3 (scoped enum holder may be missing). Resolved via getattr so the
# unscoped fallback names never appear as literal attribute accesses.

def _rb_enum(holder, scope, *names):
    """Return holder.<scope>.<name> if that scope exists, else holder.<name>."""
    inner = getattr(holder, scope, None)
    if inner is None or not all(hasattr(inner, n) for n in names if n):
        inner = holder
    return tuple(getattr(inner, n) if n else None for n in names)

# QMessageBox standard buttons
QMessageBoxYes, QMessageBoxNo = _rb_enum(QMessageBox, 'StandardButton', 'Yes', 'No')

# QgsTask flags
(QgsTaskCanCancel,) = _rb_enum(QgsTask, 'Flag', 'CanCancel')


# ── QGIS 3 / QGIS 4 compatibility helpers ────────────────────────────────────

def _rb_add_menu(iface, text, action):
    """addPluginToRasterMenu was removed in some QGIS 4 builds."""
    for method in ('addPluginToRasterMenu', 'addPluginToMenu'):
        fn = getattr(iface, method, None)
        if fn:
            fn(text, action)
            return

def _rb_remove_menu(iface, text, action):
    for method in ('removePluginRasterMenu', 'removePluginMenu'):
        fn = getattr(iface, method, None)
        if fn:
            fn(text, action)
            return

def _rb_add_map_layer(layer):
    """addMapLayer renamed/reorganised in QGIS 4."""
    project = QgsProject.instance()
    fn = getattr(project, 'addMapLayer', None)
    if fn:
        try:
            fn(layer); return
        except TypeError:
            pass
    fn = getattr(project, 'addMapLayers', None)
    if fn:
        fn([layer])


def _rb_mem_config():
    """
    Return (gdal_cachemax_mb, warp_mem_mb) scaled to this machine's physical
    RAM so GDAL uses a meaningful chunk of memory instead of a fixed ~1 GB.

    Falls back to conservative 1 GB values when total RAM can't be determined.
    """
    total_mb = None

    # Preferred: psutil (bundled with some QGIS installs)
    try:
        import psutil
        total_mb = psutil.virtual_memory().total // (1024 * 1024)
    except Exception as e:
        _rb_debug('psutil RAM probe unavailable, trying platform APIs: %s' % e)

    # Fallback: platform APIs
    if not total_mb:
        try:
            if os.name == 'nt':
                import ctypes

                class _MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                    ]

                stat = _MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                total_mb = int(stat.ullTotalPhys // (1024 * 1024))
            else:
                total_mb = int(
                    (os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'))
                    // (1024 * 1024)
                )
        except Exception:
            total_mb = None

    if not total_mb or total_mb <= 0:
        return 1024, 1024

    # Block cache: half of RAM, capped so we never starve QGIS / the OS.
    cache_mb = max(1024, min(int(total_mb * 0.5), 16384))
    # Warp operation memory: a quarter of RAM, similarly capped.
    warp_mb = max(1024, min(int(total_mb * 0.25), 8192))
    return cache_mb, warp_mb


# ── GDAL Python API helpers ─────────────────────────────────────────────────
#
# The plugin drives GDAL through osgeo.gdal rather than spawning gdal_translate
# / gdalwarp subprocesses. Benefits: a real progress callback (no stderr regex
# parsing), no dependency on the CLI tools being on PATH, no Windows command
# line length limit with large GCP sets, and no temp .vrt files on disk.

@contextmanager
def _rb_gdal_config(options):
    """Apply GDAL config options for the duration of the block, then restore."""
    previous = {}
    try:
        for key, value in options.items():
            previous[key] = gdal.GetConfigOption(key)
            gdal.SetConfigOption(key, str(value))
        yield
    finally:
        for key, value in previous.items():
            gdal.SetConfigOption(key, value)


@contextmanager
def _rb_gdal_env(num_threads, cache_mb):
    """
    Apply the resource settings (thread count + block cache) for one GDAL
    operation, restoring the previous values afterwards.
    """
    prev_cache = gdal.GetCacheMax()
    prev_threads = gdal.GetConfigOption('GDAL_NUM_THREADS')
    try:
        if cache_mb:
            gdal.SetCacheMax(int(cache_mb) * 1024 * 1024)
        gdal.SetConfigOption('GDAL_NUM_THREADS', str(num_threads))
        yield
    finally:
        gdal.SetCacheMax(prev_cache)
        gdal.SetConfigOption('GDAL_NUM_THREADS', prev_threads)


def _rb_progress_cb(task, base=0.0, span=100.0):
    """
    Build a GDAL progress callback bound to a QgsTask.

    GDAL calls it as ``callback(complete, message, cb_data)`` with ``complete``
    in 0..1. Returning 0 aborts the running GDAL operation, which is how a
    cancelled task stops the work mid-flight.
    """
    def callback(complete, message, cb_data):
        try:
            task.setProgress(base + max(0.0, min(1.0, complete)) * span)
        except Exception:  # nosec B110 - progress reporting must never abort a warp
            pass
        return 0 if task.isCanceled() else 1
    return callback


def _rb_transform_options(transform):
    """Map a UI transformation name to gdal.WarpOptions keyword arguments."""
    tl = transform.lower()
    if tl == 'tps':
        return {'tps': True}
    if tl == 'rpc':
        return {'rpc': True}
    if tl == 'geoloc':
        return {'geoloc': True}
    if tl.startswith('polynomial'):
        try:
            order = int(tl.split('order')[-1].strip().strip(')').strip())
        except ValueError:
            order = 1
        return {'polynomialOrder': order}
    return {}


def _rb_overview_levels(width, height):
    """Power-of-two overview factors down to ~256 px (gdaladdo-style defaults)."""
    longest = max(width, height)
    levels = []
    factor = 2
    while longest // factor > 256:
        levels.append(factor)
        factor *= 2
    return levels or [2]


def _rb_open_with_gcps(src_path, gcps, gcp_srs):
    """
    Open ``src_path`` and return an in-memory VRT dataset carrying ``gcps``.

    ``gcps`` is a list of ``(pixel, line, mapX, mapY)`` tuples (the line value
    is already sign-flipped by parse_points_file to match GDAL's convention).
    """
    src = gdal.Open(src_path, gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError('Cannot open input image: %s' % src_path)
    gdal_gcps = [gdal.GCP(mx, my, 0.0, px, ln) for (px, ln, mx, my) in gcps]
    vrt = gdal.Translate(
        '', src,
        options=gdal.TranslateOptions(
            format='VRT', GCPs=gdal_gcps, outputSRS=gcp_srs
        )
    )
    src = None
    if vrt is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or 'Failed to attach GCPs')
    return vrt


def _rb_cog_creation_options(compress, quality, num_threads):
    """COG driver creation options for the given compression."""
    co = [
        'COMPRESS=%s' % compress,
        'NUM_THREADS=%s' % num_threads,
        'BLOCKSIZE=512',
        'BIGTIFF=YES',
        'OVERVIEW_RESAMPLING=LANCZOS',
    ]
    if compress in ('WEBP', 'JPEG'):
        # COG driver uses QUALITY for both JPEG and WEBP (there is no WEBP_LEVEL
        # here — passing it silently left WEBP at the default quality of 75).
        co.append('QUALITY=%s' % quality)
    elif compress in ('LZW', 'DEFLATE', 'ZSTD'):
        co.append('PREDICTOR=YES')
    return co


def _rb_gtiff_creation_options(compress, quality, num_threads):
    """GTiff driver creation options for the given compression."""
    co = [
        'BIGTIFF=YES',
        'TILED=YES',
        'NUM_THREADS=%s' % num_threads,
        'COMPRESS=%s' % compress,
    ]
    if compress == 'WEBP':
        co += ['WEBP_LEVEL=%s' % quality, 'WEBP_LOSSLESS=NO']
    elif compress == 'JPEG':
        co.append('JPEG_QUALITY=%s' % quality)
    elif compress in ('LZW', 'DEFLATE', 'ZSTD'):
        # GTiff driver wants a numeric predictor (2 = horizontal, for integer
        # imagery); 'YES' is a COG-driver spelling the GTiff driver rejects.
        co.append('PREDICTOR=2')
    return co


def _rb_overview_config(compress, quality):
    """Config options so internal overviews inherit the main compression.

    Thread count is not set here — the caller's _rb_gdal_env already exports
    GDAL_NUM_THREADS, which the overview builder honours.
    """
    cfg = {'COMPRESS_OVERVIEW': compress}
    if compress == 'WEBP':
        cfg['WEBP_LEVEL_OVERVIEW'] = str(quality)
    elif compress == 'JPEG':
        cfg['JPEG_QUALITY_OVERVIEW'] = str(quality)
    elif compress in ('LZW', 'DEFLATE', 'ZSTD'):
        cfg['PREDICTOR_OVERVIEW'] = '2'
    return cfg


# Qgis message levels — the scoped MessageLevel enum on Qt6, the flat aliases
# on old Qt5. Resolved via getattr so no unscoped name appears in the source.
_ml = getattr(Qgis, 'MessageLevel', None)
if _ml is None or not hasattr(_ml, 'Info'):
    _ml = Qgis
QgisInfo     = _ml.Info
QgisWarning  = _ml.Warning
QgisCritical = _ml.Critical
QgisSuccess  = getattr(_ml, 'Success', _ml.Info)  # Success missing in very early QGIS 3


# QDialog modal loop — the trailing-underscore method name was dropped in Qt6.
def _exec_dialog(dlg):
    """Run the dialog's modal loop, preferring the Qt6 method name."""
    runner = getattr(dlg, 'exec', None)
    if runner is None:
        runner = getattr(dlg, 'exec_')
    return runner()


def _rb_debug(msg):
    """
    Low-noise diagnostic logging for best-effort code paths (window
    introspection, teardown, optional dependencies). Never raises.
    """
    try:
        QgsMessageLog.logMessage(
            'Raster Blaster: ' + str(msg), 'Raster Blaster', level=QgisInfo
        )
    except Exception:  # nosec B110 - logging itself must never break the caller
        pass


class GdalTask(QgsTask):
    """
    Background task that runs one GDAL operation off the UI thread.

    ``work_fn(task)`` performs the GDAL calls (via osgeo.gdal) and returns a
    truthy value on success. It should use ``task.isCanceled()`` and the
    progress callbacks from ``_rb_progress_cb`` so cancellation takes effect.
    """

    def __init__(self, description, work_fn, output_file=None):
        super().__init__(description, QgsTaskCanCancel)
        self._work_fn = work_fn
        self.output_file = output_file
        self.error_message = None
        self.elapsed_time = 0
        self.exception = None

    def run(self):
        """Execute the GDAL work function in a background thread."""
        start_time = time.time()
        gdal.ErrorReset()
        try:
            ok = bool(self._work_fn(self))
            self.elapsed_time = time.time() - start_time
            if not ok and not self.error_message and not self.isCanceled():
                self.error_message = gdal.GetLastErrorMsg() or 'GDAL operation failed'
            return ok
        except Exception as e:
            if self.isCanceled():
                # GDAL raises "User terminated" when the progress callback aborts
                # a cancelled run — that's expected, not an error.
                return False
            self.exception = e
            self.error_message = str(e) or gdal.GetLastErrorMsg() or repr(e)
            QgsMessageLog.logMessage(
                f'Raster Blaster: {self.error_message}',
                'Raster Blaster', level=QgisCritical
            )
            return False

    def cancel(self):
        """Handle task cancellation."""
        QgsMessageLog.logMessage(
            'Raster Blaster: Task cancelled by user',
            'Raster Blaster', level=QgisWarning
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
    SETTING_JPEG_QUALITY = 'jpeg_quality'  # also used for WEBP_LEVEL
    SETTING_MAX_THREADS = 'max_threads'    # 0 = all logical CPUs
    SETTING_CACHE_MB = 'cache_mb'          # 0 = auto (see _rb_mem_config)
    
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
        _rb_add_menu(self.iface, "&Raster Blaster", self.act_full)

        # Points→COG
        self.act_to_cog = QAction(icon, "Points→COG", self.iface.mainWindow())
        self.act_to_cog.triggered.connect(self.full_to_cog_dialog)
        _rb_add_menu(self.iface, "&Raster Blaster", self.act_to_cog)

        # GeoTIFF→COG
        self.act_cog = QAction(icon, "GeoTIFF→COG", self.iface.mainWindow())
        self.act_cog.triggered.connect(self.gdal_cog_dialog)
        _rb_add_menu(self.iface, "&Raster Blaster", self.act_cog)
        
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
                _rb_remove_menu(self.iface, "&Raster Blaster", act)
            except Exception as e:
                _rb_debug('unload: could not remove menu action: %s' % e)

        # Remove toolbar
        if hasattr(self, 'toolbar') and self.toolbar:
            try:
                del self.toolbar
            except Exception as e:
                _rb_debug('unload: could not remove toolbar: %s' % e)

    def try_connect(self):
        """Poll for Georeferencer window and add toolbar buttons when found."""
        for w in QApplication.topLevelWidgets():
            if w.metaObject().className() == 'QgsGeoreferencerMainWindow':
                QgsMessageLog.logMessage(
                    'Raster Blaster: Found Georeferencer', 
                    'Raster Blaster', level=QgisInfo
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
        except Exception as e:
            _rb_debug('georef detect (window title): %s' % e)

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
            except Exception as e:
                _rb_debug('georef detect (status bar): %s' % e)

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
            except Exception as e:
                _rb_debug('georef detect (canvas layers): %s' % e)

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
            except Exception as e:
                _rb_debug('georef detect (settings scan): %s' % e)

        # Method 5: Check our plugin's last used directory (FAST)
        if not result['input_file'] and filename_from_title:
            try:
                last_dir = self.get_setting(self.SETTING_LAST_DIR, '')
                if last_dir and os.path.isdir(last_dir):
                    potential_path = os.path.join(last_dir, filename_from_title)
                    if os.path.exists(potential_path):
                        result['input_file'] = potential_path
            except Exception as e:
                _rb_debug('georef detect (last directory): %s' % e)

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
                except Exception as e:
                    _rb_debug('points-file search failed: %s' % e)

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
                combo.addItems(['WEBP', 'JPEG', 'LZW', 'DEFLATE', 'PACKBITS', 'ZSTD', 'NONE'])
                saved = self.get_setting(self.SETTING_COMPRESSION, 'WEBP')
                idx = combo.findText(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                hl.addWidget(lbl)
                hl.addWidget(combo)
                options_layout.addLayout(hl)
                inputs[key] = combo
                continue
            
            # Quality spinbox (used for both JPEG quality and WEBP level)
            if field_type == 'jpeg_quality':
                spin = QSpinBox()
                spin.setRange(1, 100)
                spin.setValue(int(self.get_setting(self.SETTING_JPEG_QUALITY, '85')))
                spin.setSuffix('%')
                hl.addWidget(lbl)
                hl.addWidget(spin)
                options_layout.addLayout(hl)
                inputs[key] = spin
                
                # Update label dynamically based on compression selection
                def make_quality_updater(label_widget, spin_widget):
                    def update_quality_label(compress_text):
                        if compress_text == 'WEBP':
                            label_widget.setText('WEBP Quality')
                            spin_widget.setToolTip(
                                'WEBP lossy quality (1-100). 85-90 recommended for Soar. '
                                'Higher = better quality, larger file.'
                            )
                        elif compress_text == 'JPEG':
                            label_widget.setText('JPEG Quality')
                            spin_widget.setToolTip(
                                'JPEG quality (1-100). Note: JPEG re-encodes overviews lossy. '
                                'Consider WEBP for better overview quality.'
                            )
                        is_lossy = compress_text in ('WEBP', 'JPEG')
                        spin_widget.setVisible(is_lossy)
                        label_widget.setVisible(is_lossy)
                    return update_quality_label
                
                updater = make_quality_updater(lbl, spin)
                
                # Connect to compress combo if it exists
                if 'compress' in inputs:
                    inputs['compress'].currentTextChanged.connect(updater)
                    # Set initial state
                    updater(inputs['compress'].currentText())
                
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

        # Performance group — how much of the machine GDAL is allowed to use.
        perf_group = QGroupBox("Performance")
        perf_layout = QVBoxLayout()

        cpu_count = os.cpu_count() or 1
        auto_cache_mb = _rb_mem_config()[0]

        thr_hl = QHBoxLayout()
        thr_lbl = QLabel("CPU threads")
        thr_lbl.setMinimumWidth(120)
        threads_spin = QSpinBox()
        threads_spin.setRange(0, cpu_count)
        threads_spin.setSpecialValueText(f"All ({cpu_count})")
        try:
            threads_spin.setValue(int(self.get_setting(self.SETTING_MAX_THREADS, '0') or 0))
        except (TypeError, ValueError):
            threads_spin.setValue(0)
        threads_spin.setToolTip(
            "Threads GDAL may use for warping and (de)compression.\n"
            "0 = all logical CPUs. Lower it to keep cores free for other work."
        )
        thr_hl.addWidget(thr_lbl)
        thr_hl.addWidget(threads_spin)
        perf_layout.addLayout(thr_hl)
        inputs['_threads'] = threads_spin

        cache_hl = QHBoxLayout()
        cache_lbl = QLabel("GDAL cache (MB)")
        cache_lbl.setMinimumWidth(120)
        cache_spin = QSpinBox()
        cache_spin.setRange(0, 65536)
        cache_spin.setSingleStep(256)
        cache_spin.setSpecialValueText(f"Auto ({auto_cache_mb})")
        try:
            cache_spin.setValue(int(self.get_setting(self.SETTING_CACHE_MB, '0') or 0))
        except (TypeError, ValueError):
            cache_spin.setValue(0)
        cache_spin.setToolTip(
            "GDAL block cache size.\n"
            "0 = auto (half of system RAM, capped at 16 GB)."
        )
        cache_hl.addWidget(cache_lbl)
        cache_hl.addWidget(cache_spin)
        perf_layout.addLayout(cache_hl)
        inputs['_cache_mb'] = cache_spin

        perf_group.setLayout(perf_layout)
        layout.addWidget(perf_group)

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
            self.save_setting(self.SETTING_MAX_THREADS, str(threads_spin.value()))
            self.save_setting(self.SETTING_CACHE_MB, str(cache_spin.value()))
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

            # Resource controls, resolved to concrete values for the GDAL calls.
            auto_cache, auto_warp = _rb_mem_config()
            threads_val = threads_spin.value()
            values['num_threads'] = 'ALL_CPUS' if threads_val <= 0 else str(threads_val)
            cache_val = cache_spin.value()
            values['cache_mb'] = cache_val if cache_val > 0 else auto_cache
            values['warp_mb'] = auto_warp

            callback(values)
        
        run_btn.clicked.connect(on_run)
        btn_layout.addWidget(run_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        
        dlg.setLayout(layout)
        
        _exec_dialog(dlg)

    # =========================================================================
    # GeoTIFF → COG
    # =========================================================================
    
    def gdal_cog_dialog(self):
        self._gdal_dialog('GeoTIFF → COG', [
            ('Input GeoTIFF', 'input_file', 'input_file'),
            ('Compression', 'compress', 'compress'),
            ('Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output COG', 'output_file', 'output_cog')
        ], self.gdal_cog)

    def _run_task(self, task, values, out_path, label):
        """
        Wire a GdalTask to the dialog: show progress, react on completion,
        auto-load the result, and keep the task alive while it runs.
        """
        progress = values['progress']
        status = values['status']
        dlg = values['dialog']
        run_btn = values['run_button']
        auto_load = values['auto_load']

        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setVisible(True)
        status.setText("Processing...")
        run_btn.setEnabled(False)

        def on_progress(pct):
            try:
                progress.setValue(int(pct))
            except RuntimeError:
                pass  # dialog was closed

        def on_finished():
            try:
                self.active_tasks.remove(task)
            except ValueError:
                pass
            try:
                progress.setVisible(False)
                run_btn.setEnabled(True)
            except RuntimeError:
                return  # dialog gone; nothing left to update

            if task.isCanceled():
                status.setText("Cancelled")
                return

            if task.error_message:
                status.setText(f"Error: {task.error_message[:100]}")
                QgsMessageLog.logMessage(
                    f'Raster Blaster: {label} failed: {task.error_message}',
                    'Raster Blaster', level=QgisCritical
                )
                self.iface.messageBar().pushMessage(
                    "Raster Blaster", f"{label} creation failed",
                    level=QgisCritical
                )
                return

            mins, secs = divmod(int(task.elapsed_time), 60)
            status.setText(f"Complete! ({mins:02d}:{secs:02d})")
            self.iface.messageBar().pushMessage(
                "Raster Blaster",
                f"{label} created: {os.path.basename(out_path)} ({mins:02d}:{secs:02d})",
                level=QgisSuccess
            )
            if auto_load:
                self.load_raster_layer(out_path)
            try:
                dlg.accept()
            except RuntimeError:
                pass

        task.progressChanged.connect(on_progress)
        task.taskCompleted.connect(on_finished)
        task.taskTerminated.connect(on_finished)

        QgsApplication.taskManager().addTask(task)
        self.active_tasks.append(task)

    def gdal_cog(self, values):
        """Convert an existing GeoTIFF to a COG (GDAL COG driver, off-thread)."""
        tif = values['input_file']
        compress = values['compress']
        quality = values['jpeg_quality']
        cog = values['output_file']
        dlg = values['dialog']
        num_threads = values['num_threads']
        cache_mb = values['cache_mb']

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
            try:
                os.remove(cog)
            except Exception as e:
                QMessageBox.critical(dlg, "Error", f"Cannot delete existing file:\n{e}")
                return

        # Alpha bands are left intact — the COG driver handles transparency for
        # every compression (JPEG via an internal mask, others as a real band).
        creation_opts = _rb_cog_creation_options(compress, quality, num_threads)

        def work(task):
            with _rb_gdal_env(num_threads, cache_mb):
                out = gdal.Translate(
                    cog, tif,
                    options=gdal.TranslateOptions(
                        format='COG',
                        creationOptions=creation_opts,
                        callback=_rb_progress_cb(task),
                    )
                )
                ok = out is not None
                out = None
            return ok

        self._run_task(
            GdalTask('Raster Blaster: Creating COG', work, output_file=cog),
            values, cog, 'COG'
        )

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
            ('Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output GeoTIFF', 'output_file', 'output_geotiff')
        ], self.full_process, initial_values)

    def full_process(self, values):
        """Georeference points + image into a plain GeoTIFF (off-thread)."""
        pf = values['points_file']
        tif = values['input_file']
        crs = values['crs']
        transform = values['transform']
        resample = values['resample']
        compress = values['compress']
        quality = values['jpeg_quality']
        out_tif = values['output_file']
        dlg = values['dialog']
        num_threads = values['num_threads']
        cache_mb = values['cache_mb']
        warp_mb = values['warp_mb']

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
        
        # Alpha bands are left intact — gdalwarp preserves transparency (JPEG
        # via an internal mask, other compressions as a real band).
        transform_opts = _rb_transform_options(transform)
        creation_opts = _rb_gtiff_creation_options(compress, quality, num_threads)
        overview_cfg = _rb_overview_config(compress, quality)
        # authid() is empty for a custom/user CRS — fall back to full WKT.
        dst_srs = crs.authid() or crs.toWkt()
        gcps = gcp_data['gcps']

        def work(task):
            with _rb_gdal_env(num_threads, cache_mb):
                # In-memory VRT carrying the GCPs (no temp .vrt on disk).
                vrt = _rb_open_with_gcps(tif, gcps, dst_srs)
                if task.isCanceled():
                    return False
                warp_opts = gdal.WarpOptions(
                    format='GTiff',
                    dstSRS=dst_srs,
                    resampleAlg=resample,
                    multithread=True,
                    warpMemoryLimit=warp_mb,
                    warpOptions=['NUM_THREADS=%s' % num_threads],
                    creationOptions=creation_opts,
                    callback=_rb_progress_cb(task, 0.0, 90.0),
                    **transform_opts
                )
                out = gdal.Warp(out_tif, vrt, options=warp_opts)
                vrt = None
                if out is None:
                    return False
                # Plain GeoTIFF gets no overviews from gdalwarp — build them so
                # the result draws quickly in QGIS. A failure here still leaves
                # a valid (overview-less) file, so don't fail the whole task.
                if not task.isCanceled():
                    levels = _rb_overview_levels(out.RasterXSize, out.RasterYSize)
                    try:
                        with _rb_gdal_config(overview_cfg):
                            out.BuildOverviews(
                                'LANCZOS', levels,
                                callback=_rb_progress_cb(task, 90.0, 10.0)
                            )
                    except RuntimeError as e:
                        QgsMessageLog.logMessage(
                            f'Raster Blaster: overview build failed: {e}',
                            'Raster Blaster', level=QgisWarning
                        )
                out = None
            return not task.isCanceled()

        self._run_task(
            GdalTask('Raster Blaster: Creating GeoTIFF', work, output_file=out_tif),
            values, out_tif, 'GeoTIFF'
        )

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
            ('Quality', 'jpeg_quality', 'jpeg_quality'),
            ('Output COG', 'output_file', 'output_cog')
        ], self.full_to_cog, initial_values)

    def full_to_cog(self, values):
        """Georeference points + image straight into a COG (off-thread)."""
        pf = values['points_file']
        tif = values['input_file']
        crs = values['crs']
        transform = values['transform']
        resample = values['resample']
        compress = values['compress']
        quality = values['jpeg_quality']
        out_cog = values['output_file']
        dlg = values['dialog']
        num_threads = values['num_threads']
        cache_mb = values['cache_mb']
        warp_mb = values['warp_mb']

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
        
        # Alpha bands are left intact — the COG driver handles transparency for
        # every compression (JPEG via an internal mask, others as a real band).
        transform_opts = _rb_transform_options(transform)
        creation_opts = _rb_cog_creation_options(compress, quality, num_threads)
        # authid() is empty for a custom/user CRS — fall back to full WKT.
        dst_srs = crs.authid() or crs.toWkt()
        gcps = gcp_data['gcps']

        def work(task):
            with _rb_gdal_env(num_threads, cache_mb):
                # In-memory VRT carrying the GCPs (no temp .vrt on disk).
                vrt = _rb_open_with_gcps(tif, gcps, dst_srs)
                if task.isCanceled():
                    return False
                warp_opts = gdal.WarpOptions(
                    format='COG',
                    dstSRS=dst_srs,
                    resampleAlg=resample,
                    multithread=True,
                    warpMemoryLimit=warp_mb,
                    warpOptions=['NUM_THREADS=%s' % num_threads],
                    creationOptions=creation_opts,
                    callback=_rb_progress_cb(task),
                    **transform_opts
                )
                out = gdal.Warp(out_cog, vrt, options=warp_opts)
                vrt = None
                ok = out is not None
                out = None
            return ok and not task.isCanceled()

        self._run_task(
            GdalTask('Raster Blaster: Creating COG', work, output_file=out_cog),
            values, out_cog, 'COG'
        )

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
                    'Raster Blaster', level=QgisWarning
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
                _rb_add_map_layer(layer)
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Added layer "{name}" to map',
                    'Raster Blaster', level=QgisInfo
                )
            else:
                QgsMessageLog.logMessage(
                    f'Raster Blaster: Failed to load layer from {filepath}',
                    'Raster Blaster', level=QgisWarning
                )
        except Exception as e:
            QgsMessageLog.logMessage(
                f'Raster Blaster: Error loading layer: {e}',
                'Raster Blaster', level=QgisWarning
            )