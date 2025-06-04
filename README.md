# Raster Blaster

*This plugin is experimental. I have almost zero coding experience but I decided to create this plugin for my projects and thought others would benefit from it. ChatGPT did help me put this together. Please don't shame me, it's just a tool that allows a non-coding person like me to make this :)*

A QGIS plugin that streamlines your raster georeferencing workflow by embedding GDAL’s powerful command-line tools directly into the Georeferencer interface. This means it'll make use of your computers system resources more than QGIS normally would. It adds buttons for every step—from reading your GCPs through to generating Cloud-Optimized GeoTIFFs (COGs)—all without ever leaving QGIS.

This also allows people who don't know how to use code or command line interfaces to use th power of GDAL to speed up their work flows.


## The Plugin

This plugin adds three buttons to the QGIS Georeferencer toolbar, as well as under the main QGIS toolbar (Plugins>Georeference with GDAL):


**Points→GeoTIFF**: Reads your .points file (GCPs) and the input file, creates an intermediate VRT, then warps it to a projected GeoTIFF (EPSG:3857).


**Points→COG**: Same as Points→GeoTIFF, but warps directly into a Cloud-Optimized GeoTIFF (COG).


**GeoTIFF→COG**: Converts an existing GeoTIFF into a COG.


*Note: When these processes are running, QGIS often becomes unresponsive. Just be patient and let the task complete as it always comes back once the process is complete in my testing.



### Other notes

**Transformation, Resample, and Compression options**


Eaach process gives you mutliple options for transformation types (when georeferencing), resampling types, and compression types. I have these preset to my preferred options, but of course you can select which ever you prefer.


**Single-window dialogs**


 Each process pops up a simple file-selection dialog, letting you browse for your GCP file, source TIFF, choose transform, compression, and resampling types, and choose an output path—no messy command-line parameters.


**Running GDAL Commands**


When you click Points→GeoTIFF or Points→COG, it:  
- Reads and filters your .points file (skipping comments).  
- Constructs a list of -gcp arguments from enabled GCP rows.  
- Writes out a temporary VRT with gdal_translate -of VRT ….  
- Calls gdalwarp with EPSG:3857 reprojection, Lanczos resampling, tiling/compression options, and a 16 GB cache.  
- Points→COG instead uses gdalwarp -of COG … so no intermediate GeoTIFF is ever written.  
- GeoTIFF→COG simply invokes gdal_translate -of COG … on an existing TIFF.  



