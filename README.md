# Raster Blaster

Raster Blaster is a QGIS plugin that streamlines your raster georeferencing workflow by embedding GDAL's powerful command-line tools directly into the Georeferencer interface. It adds buttons for every step—from reading your GCPs .points file through to generating Cloud-Optimized GeoTIFFs (COGs)—all without ever leaving QGIS.

This plugin allows people who don't know how to use code or command line interfaces to harness the full power of GDAL to speed up their workflows.

## Features

### Three Core Functions

- **Points→GeoTIFF**: Reads your .points file (GCPs) and input image, creates an intermediate VRT, then warps it to a projected GeoTIFF in your chosen CRS.
- **Points→COG**: Same as Points→GeoTIFF, but outputs directly to a Cloud-Optimized GeoTIFF (COG).
- **GeoTIFF→COG**: Converts an existing GeoTIFF into a COG.

### Background Processing
All GDAL operations run in a background thread, keeping QGIS fully responsive during processing. You can continue working while your rasters process, and even queue multiple tasks.

### Flexible Options

**Target CRS**: Choose any coordinate reference system—no longer hardcoded. Pick from recent CRS, search by name/code, or browse the full list.

**Transformation Types**:
- TPS (Thin Plate Spline)
- RPC
- Geoloc
- Polynomial (order 1, 2, or 3)

**Resampling Methods**:
- lanczos (default - best quality)
- near (fastest)
- bilinear, cubic, cubicspline
- average, mode
- max, min, med

**Compression Options**:
- JPEG (with adjustable quality 1-100%)
- LZW, DEFLATE, PACKBITS, ZSTD
- NONE

### Smart Validation

The plugin checks your GCPs before processing and warns you about potential issues:

- **GCP count validation**: Ensures you have enough ground control points for your chosen transformation method (e.g., Polynomial order 2 needs at least 6 GCPs)
- **Distribution warnings**: Alerts you if GCPs are clustered in one area or arranged in a straight line, which can cause poor results

### Quality of Life

- **Persistent settings**: Your preferences (compression, resampling, CRS, etc.) are saved between sessions
- **Auto-fill output path**: Selecting an input image automatically suggests an output filename
- **Auto-load results**: Optionally add the processed raster directly to your map
- **Overwrite protection**: Prompts for confirmation before overwriting existing files
- **Progress tracking**: See processing status in real-time

## Installation

1. Download the plugin as a ZIP file from GitHub (Code → Download ZIP)
2. Open QGIS
3. Go to **Plugins → Manage and Install Plugins → Install from ZIP**
4. Select the downloaded ZIP file
5. Click **Install Plugin**

## Usage

Access the plugin from:
- **Menu**: Raster → Raster Blaster → [choose function]
- **Georeferencer toolbar**: Buttons are added automatically when the Georeferencer window is open

### Basic Workflow

1. Open the Georeferencer and add your GCPs as usual
2. Save your GCPs to a .points file
3. Click **Points→GeoTIFF** or **Points→COG**
4. Select your points file and input image (output path auto-fills)
5. Choose your target CRS and processing options
6. Click **Run**

### Tips

- **Large files**: Processing may take several minutes for large rasters. The progress indicator keeps you informed.
- **Compression choice**: Use JPEG for aerial/satellite imagery; LZW or DEFLATE for maps with solid colors or text.
- **GCP quality**: More GCPs generally means better results, but distribution matters more than quantity. Spread them across corners and edges.
- **Check the log**: If something goes wrong, check **View → Panels → Log Messages** and look for "Raster Blaster" entries.

## How It Works

When you run Points→GeoTIFF or Points→COG:

1. Parses your .points file (supports UTF-8 and Windows-1252 encoding)
2. Validates GCP count and distribution for your chosen transformation
3. Builds GCP arguments for GDAL
4. Creates a temporary VRT with `gdal_translate -of VRT`
5. Runs `gdalwarp` with your chosen CRS, resampling, and compression
6. Cleans up temporary files
7. Optionally loads the result into QGIS

The plugin uses multi-threading (`GDAL_NUM_THREADS=ALL_CPUS`) to maximize performance.

## Requirements

- QGIS 3.0 or later (compatible with both Qt5 and Qt6 versions)
- GDAL (included with QGIS)

## License

This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation; either version 2 of the License, or (at your option) any later version.

## Support

- **Issues**: https://github.com/harryspacefromspace/rasterblaster/issues
- **Repository**: https://github.com/harryspacefromspace/rasterblaster

## Credits

Created by Harry Stranger (harry@spacefromspace.com)
