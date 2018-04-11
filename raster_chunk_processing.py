#*****************************************************************************
#
#  Project:  Parallel Raster Chunk Processing
#  Purpose:  Applies various raster processes (various smoothing algorithms,
#            etc) to arbitrarily large rasters by chunking it out into smaller
#            pieces and processes in parallel (if desired)
#  Author:   Jacob Adams, jacob.adams@cachecounty.org
#
#*****************************************************************************
# MIT License
#
# Copyright (c) 2018 Cache County
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#*****************************************************************************

import numpy as np
import datetime
import os
import math
import subprocess
import contextlib
import tempfile
import warnings
import csv
import multiprocessing as mp
#from scipy.signal import fftconvolve
from astropy.convolution import convolve_fft
from skimage import exposure
from osgeo import gdal
from scipy.ndimage.filters import generic_filter as gf

# Just a simple class to hold the information about each chunk
class Chunk:
    pass

def sizeof_fmt(num, suffix='B'):
    '''
    Quick-and-dirty method for formating file size, from Sridhar Ratnakumar,
    https://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size.
    '''
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'Yi', suffix)

def WriteASC(in_array, asc_path, xll, yll, c_size, nodata=-37267):
    '''
    Writes an np.array to a .asc file, which is the most accessible format for
    mdenoise.exe.
    in_array:       The input array, should be read using the supper_array
                    technique from below.
    asc_path:       The output path for the .asc file
    xll:            X coordinate for lower left corner; actual position is
                    irrelevant for mdenoise blur method below.
    y11:            Y coordinate for lower left corner; see above.
    c_size:         Square dimension of raster cell.
    nodata:         NoData value for .asc file.
    '''

    rows = in_array.shape[0]
    cols = in_array.shape[1]
    ncols = "ncols %d\n" %cols
    nrows = "nrows %d\n" %rows
    xllcorner = "xllcorner %f\n" %xll
    yllcorner = "yllcorner %f\n" %yll
    cellsize = "cellsize %f\n" %c_size
    nodata_value = "nodata_value %f\n" %nodata

    with open(asc_path, 'w') as f:
        # Write Header
        f.write(ncols)
        f.write(nrows)
        f.write(xllcorner)
        f.write(yllcorner)
        f.write(cellsize)
        f.write(nodata_value)

        # Write data
        for i in range(rows):
            row = " ".join("{0}".format(n) for n in in_array[i, :])
            f.write(row)
            f.write("\n")

def blur_mean(in_array, filter_size):
    '''
    Performs a simple blur based on the average of nearby values. Uses circular
    mask from Inigo Hernaez Corres, https://stackoverflow.com/questions/8647024/how-to-apply-a-disc-shaped-mask-to-a-numpy-arrayself.
    This is the equivalent of ArcGIS' Focal Mean Statistics raster processing
    tool.
    in_array:       The input array, should be read using the supper_array
                    technique from below.
    filter_size:    The diameter (in grid cells) of the circle used to define
                    nearby pixels. A larger value creates more pronounced
                    smoothing.
    '''

    # Using circular mask from user Inigo Hernaez Corres, https://stackoverflow.com/questions/8647024/how-to-apply-a-disc-shaped-mask-to-a-numpy-array
    radius = math.floor(filter_size/2)
    kernel = np.zeros((2*radius+1, 2*radius+1))
    y,x = np.ogrid[-radius:radius+1, -radius:radius+1]
    mask = x**2 + y**2 <= radius**2
    kernel[mask] = 1
    circular_mean = gf(in_array, np.mean, footprint=kernel)

    return circular_mean

def blur_gauss(in_array, size):
    '''
    Performs a guassian blur on an array of elevations. Modified from Mike
    Toews, https://gis.stackexchange.com/questions/9431/what-raster-smoothing-generalization-tools-are-availableself.
    in_array:       The input array, should be read using the supper_array
                    technique from below.
    size:           The size (in grid cells) of the gaussian blur kernel
    '''

    # This comment block is old and left here for posterity
    # Change all NoData values to mean of valid values to fix issues with
    # massive (float32.max) NoData values completely overwhelming other array
    # data. Using mean instead of 0 gives a little bit more usable data on
    # edges.
    # Create masked array to get mean of valid data
    # masked_array = np.ma.masked_values(in_array, s_nodata)
    # array_mean = masked_array.mean()
    # # Create new array that will have NoData values replaced by array_mean
    # cleaned_array = np.copy(in_array)
    # np.putmask(cleaned_array, cleaned_array==s_nodata, array_mean)

    # Create new array with s_nodata values set to np.nan (for edges of raster)
    nan_array = np.where(in_array == s_nodata, np.nan, in_array)

    # expand cleaned_array to fit edge of kernel
    #padded_array = np.pad(nan_array, size, 'symmetric')
    # build kernel (Gaussian blur function)
    x, y = np.mgrid[-size:size + 1, -size:size + 1]
    g = np.exp(-(x**2 / float(size) + y**2 / float(size)))
    g = (g / g.sum()).astype(nan_array.dtype)
    # Convolve the data and Gaussian function (do the Gaussian blur)
    #smoothed = fftconvolve(padded_array, g, mode="valid")
    # Supressing runtime warnings due to NaNs (they just get hidden by NoData
    # masks in the supper_array rebuild anyways)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        # Use the astropy function because fftconvolve does not like np.nan
        smoothed = convolve_fft(nan_array, g, nan_treatment='interpolate')

    return smoothed

def mdenoise(in_array, t, n, v, tile=None):
    '''
    Smoothes an array of elevations using the mesh denoise algorithm by Sun et
    al (2007), Fast and Effective Feature-Preserving Mesh Denoising
    (http://www.cs.cf.ac.uk/meshfiltering/index_files/Page342.htm).
    in_array:       The input array, should be read using the supper_array
                    technique from below.
    t:              Threshold parameter for mdenoise.exe; range [0,1]
    n:              Normal updating iterations for mdenoise; try between 10
                    and 50. Larger values increase smoothing effect and runtime
    v:              Vertext updating iterations for mdenoise; try between 10
                    and 90. Appears to affect what level of detail is smoothed
                    away.
    tile:           The name of the tile (optional). Used to differentiate the
                    temporary files' filenames.
    '''
    # Implements mdenoise algorithm by Sun et al (2007)
    # The stock mdenoise.exe runs out of memory with a window size of somewhere
    # between 1500 and 2000 (with a filter size of 15, which gives a total
    # array of window + 4 * filter). Recompiling mdenoise from source on a
    # 64-bit platform may solve this.

    # Should rewrite to use tempfile.gettempdir() to get the directory rather
    # than using a user-specified dir
    # (Or really could just bite the bullet and rewrite/link mdenoise into
    # python so that we can just pass the np.array directly)

    # Nodata Masking:
    # nd values get passed to mdenoise via array
    # Return array has nd values mostly intact except for some weird burrs that
    # need to be trimmed for sake of contours (done in smooth() by copying over
    # nodata values as mask, not in here)

    # Should be multiprocessing safe; source and target files identified with
    # pid or tile in the file name, no need for locking.

    # If the file is empty (all NoData), just return the original array
    if in_array.mean() == s_nodata:
        return in_array

    # Set up paths
    temp_dir = tempfile.gettempdir()
    if tile:  # If we have a tile name, use that for differentiator
        temp_s_path = os.path.join(temp_dir, "mesh_source_{}.asc".format(tile))
        temp_t_path = os.path.join(temp_dir, "mesh_target_{}.asc".format(tile))
    else:  # Otherwise, use the pid
        pid = mp.current_process().pid
        temp_s_path = os.path.join(temp_dir, "mesh_source_{}.asc".format(pid))
        temp_t_path = os.path.join(temp_dir, "mesh_target_{}.asc".format(pid))

    # Write array to temporary ESRI ascii file
    WriteASC(in_array, temp_s_path, 1, 1, cell_size, s_nodata)

    # Call mdenoise on temporary file
    args = (mdenoise_path, "-i", temp_s_path, "-t", str(t), "-n", str(n),
            "-v", str(v), "-o", temp_t_path)
    mdenoise_output = subprocess.check_output(args, shell=False,
                                              universal_newlines=True)
    if verbose:
        print(mdenoise_output)

    # Read resulting asc file into numpy array, pass back to caller
    temp_t_fh = gdal.Open(temp_t_path, gdal.GA_ReadOnly)
    temp_t_band = temp_t_fh.GetRasterBand(1)
    mdenoised_array = temp_t_band.ReadAsArray()

    # Clean up after ourselves
    temp_t_fh = None
    temp_t_band = None

    with contextlib.suppress(FileNotFoundError):
        os.remove(temp_s_path)
        os.remove(temp_t_path)

    return(mdenoised_array)

def hillshade(in_array, az, alt): #c_size):
    # # This method has not been updated for multiprocessing; left as a
    # # placeholder for future sky model method.
    # temp_rows = in_array.shape[0]
    # temp_cols = in_array.shape[1]
    #
    # temp_dir = tempfile.gettempdir()
    #
    # mem_s_fh = gdal.GetDriverByName("MEM").Create('', temp_cols, temp_rows, 1, gdal.GDT_Float32)
    # mem_s_fh.SetGeoTransform([0, cell_size, 0, 0, 0, cell_size])
    # s_band = mem_s_fh.GetRasterBand(1)
    # s_band.SetNoDataValue(s_nodata)
    # s_band.WriteArray(in_array)
    # # ==== NOT MULTI-PROCESS SAFE !!! ====
    # hs_t_file = os.path.join(temp_dir, "hs_temp.tif")
    #
    # # Default azimuth value not quite working right.
    # # For whatever reason, Azimuth must be modified as 180 - az (if <0, +360)
    # # So for default of 315, pass 225 to DEMProcessing
    # shade = gdal.DEMProcessing(hs_t_file, mem_s_fh, "hillshade", azimuth=225.0, zFactor=1.0, altitude=45.0, combined=True).ReadAsArray()
    #
    # # Currently returning as a float to handle input NoData as float. If we ever actually used this method, we'd probably want to handle this differently, perhaps by scaling the values to 1-255 and changing NoData to 0 (like the cli hillshade command)
    # return shade.astype(float)

    # Create new array with s_nodata values set to np.nan (for edges of raster)
    nan_array = np.where(in_array == s_nodata, np.nan, in_array)

    x = np.zeros(nan_array.shape)
    y = np.zeros(nan_array.shape)

    # Conversion between mathematical and nautical azimuth
    az = 90. - az

    azrad = az * np.pi / 180.
    altrad = alt * np.pi / 180.

    x, y = np.gradient(nan_array, cell_size, cell_size, edge_order=2)

    sinalt = np.sin(altrad)
    cosaz = np.cos(azrad)
    cosalt = np.cos(altrad)
    sinaz = np.sin(azrad)
    xx_plus_yy = x*x + y*y
    shaded = (sinalt - (y * cosaz * cosalt - x * sinaz * cosalt)) / np.sqrt(1+xx_plus_yy)

    #shaded = (np.sin(altrad) -
    #         (y * np.cos(azrad) * np.cos(altrad) - x * np.sin(azrad) * np.cos(altrad))) / np.sqrt(1+(x*x + y*y))

    return shaded * 255

def skymodel(in_array, lum_lines):

    # initialize skyshade as 0's
    skyshade = np.zeros((in_array.shape))

    # Loop through luminance file lines to calculate multiple hillshades
    for line in lum_lines:
        az = float(line[0])
        alt = float(line[1])
        weight = float(line[2])

        shade = hillshade(in_array, az=az, alt=alt) * weight

        skyshade = skyshade + shade
        shade = None
    return skyshade

def TPI(in_array, filter_size):
    '''
    Returns an array of the Topographic Position Index of each cell (the
    difference between the cell and the average of its neighbors).
    in_array:       The input array, should be read using the supper_array
                    technique from below.
    filter_size:    The size, in cells, of the neighborhood used for the average
                    (uses a circular window)
    '''

    # Change all NoData values to mean of valid values to fix issues with
    # massive (float32.max) NoData values completely overwhelming other array
    # data. Using mean instead of 0 gives a little bit more usable data on
    # edges.
    # This gets us 80% of the way there. For even better, we could use a mask
    # and replace nodata with the average pixel value of all pixels within
    # a 100-pixel radius, which would give a more area-appropriate mean value
    # as elevation is usually spatially autocorrelated.
    # Or just use np.nan

    # Create new array with s_nodata values set to np.nan (for edges of raster)
    nan_array = np.where(in_array == s_nodata, np.nan, in_array)

    # Using circular mask from user Inigo Hernaez Corres, https://stackoverflow.com/questions/8647024/how-to-apply-a-disc-shaped-mask-to-a-numpy-array
    radius = math.floor(filter_size/2)
    kernel = np.zeros((2*radius+1, 2*radius+1))
    y,x = np.ogrid[-radius:radius+1, -radius:radius+1]
    mask = x**2 + y**2 <= radius**2  # pythagorean theorem check for circle
    kernel[mask] = 1  # Creates circle mask in kernel using indexes from ogrid
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        circular_mean = gf(nan_array, np.nanmean, footprint=kernel)

    return nan_array - circular_mean

def ProcessSuperArray(chunk_info):
    '''
    Given starting and ending indices of a chunk, overlap value, and relevant
    raster file info via the chunk_info object, this function calculates the
    indices of a "super array" that is 'overlap'-values larger than the chunk
    in each dimension (-x, x, -y, y). It automatically computes edge conditions
    for chunks on the edges of the original raster. It then calls the specified
    method on this super array, masks out the overlap areas on the resulting
    array, and writes the processed chunk to the output file.

    chunk_info:     A simple Chunk() data structure obejct that holds the
                    information about the chunk and the file as a whole.
                    pool.map() iterates over a single collection, so this
                    function uses a single picklable object to easily pass all
                    the needed info to the function.
    '''

    # Unpack chunk-specific info
    tile = chunk_info.tile
    progress = chunk_info.progress
    total_chunks = chunk_info.total_chunks
    x_start = chunk_info.x_start
    y_start = chunk_info.y_start
    x_end = chunk_info.x_end
    y_end = chunk_info.y_end

    # Unpack general info
    source_dem_path = chunk_info.in_dem_path
    target_dem_path = chunk_info.out_dem_path

    f2 = chunk_info.f2

    rows = chunk_info.rows
    cols = chunk_info.cols

    method = chunk_info.method
    options = chunk_info.options  # dictionary of options

    starttime = chunk_info.start_time

    # Being lazy, setting these as global so I don't have to alter the
    # processing method signatures
    global s_nodata
    global cell_size
    global verbose
    s_nodata = chunk_info.s_nodata
    cell_size = chunk_info.cell_size
    verbose = chunk_info.verbose

    # Super array calculations
    # Non-edge-case values for super array
    # f2 is our doubled overlap value; we multipy by 2 here to get an overlap
    # on each side of the dimension (ie, f2 <> x values <> f2)
    x_size = x_end - x_start + 2 * f2
    y_size = y_end - y_start + 2 * f2
    x_off = x_start - f2
    y_off = y_start - f2

    # Values for ReadAsArray, these aren't changed later unelss the border case
    # checks change them
    read_x_off = x_off
    read_y_off = y_off
    read_x_size = x_size
    read_y_size = y_size

    # Slice values (of super_array) for copying read_array in to super_array,
    # these aren't changed later unelss the border case checks change them
    sa_x_start = 0
    sa_x_end = x_size
    sa_y_start = 0
    sa_y_end = y_size

    # Edge logic
    # If super_array exceeds bounds of image:
    #   Adjust x/y offset to appropriate place (for < 0 cases only).
    #   Reduce read size by f2 (we're not reading that edge area on one side)
    #   Move start or end value for super_array slice by f2
    # Each of x/y can only be under (start of row/col) or over (end of row/col)
    #   ie, the input raster must be broken into at least two chunks in each
    #   direction
    # Checks both x and y, setting read and slice values for each dimension if
    # needed
    if x_off < 0:
        read_x_off = 0
        read_x_size = x_size - f2
        sa_x_start = f2
    elif x_off + x_size > cols:
        read_x_size = x_size - f2
        sa_x_end = -1 * f2

    if y_off < 0:
        read_y_off = 0
        read_y_size = y_size - f2
        sa_y_start = f2
    elif y_off + y_size > rows:
        read_y_size = y_size - f2
        sa_y_end = -1 * f2

    percent = (progress / total_chunks) * 100
    elapsed = datetime.datetime.now() - starttime
    print("Tile {0}: {1:d} of {2:d} ({3:0.3f}% in {4})".format(tile, progress,
                                                        total_chunks, percent,
                                                        elapsed))

    # We perform the read calls within the multiprocessing portion to avoid
    # passing the entire raster to each process. This means we need to acquire
    # a lock prior to reading in the chunk so that we're not trying to read
    # the file at the same time.
    with lock:
        # ===== LOCK HERE =====
        # Open source file handle
        # print("Opening {0:s}...".format(in_dem_path))
        s_fh = gdal.Open(source_dem_path, gdal.GA_ReadOnly)

        # Master read call. read_ variables have been changed for edge cases
        # if needed
        read_array = s_fh.ReadAsArray(read_x_off, read_y_off,
                                      read_x_size, read_y_size)
        # Arrays are of form [rows, cols], thus [y, x] when slicing

        s_fh = None
        # ===== UNLOCK HERE =====

    # Array holding superset of actual desired window, initialized to NoData
    # value.
    # Edge case logic insures edges fill appropriate portion when loaded in
    # super_array must be of type float for fftconvolve
    super_array = np.full((y_size, x_size), s_nodata)

    # The cells of our NoData-intiliazed super_array corresponding to the
    # read_array are replaced with data from read_array. This changes every
    # value, except for edge cases that leave portions of the super_array
    # as NoData.
    if verbose:
        print("Tile {} indices: [{}:{}, {}:{}]".format(tile, read_y_off,
                                                     read_y_off + read_y_size,
                                                     read_x_off,
                                                     read_x_off + read_y_size))
    super_array[sa_y_start:sa_y_end, sa_x_start:sa_x_end] = read_array

    # Do something with the data
    if method == "blur_gauss":
        new_data = blur_gauss(super_array, options["filter_size"])
    elif method == "mdenoise":
        new_data = mdenoise(super_array, options["t"],
                            options["n"], options["v"], tile)
    elif method == "clahe":
        new_data = exposure.equalize_adapthist(super_array.astype(int),
                                               options["filter_size"],
                                               options["clip_limit"])
    elif method == "TPI":
        new_data = TPI(super_array, options["filter_size"])
    elif method == "blur_mean":
        new_data = blur_mean(super_array, options["filter_size"])
    elif method == "hillshade":
        new_data = hillshade(super_array, options["az"], options["alt"])
    elif method == "skymodel":
        new_data = skymodel(super_array, options["lum_lines"])
    else:
        raise NotImplementedError("Method not implemented: %s" %method)

    # Resulting array is a superset of the data; we need to strip off the
    # overlap before writing it
    temp_array = new_data[f2:-1*f2, f2:-1*f2]

    # slice down super_array to get original chunk of data (ie, super_array
    # minus additional data on edges) to use for finding NoData areas
    read_sub_array = super_array[f2:-f2, f2:-f2]

    # Reset NoData values in our result to match the NoData areas in the source
    # array (areas in temp_array where corresponding cells in
    # read_sub_array==NoData get set to s_nodata)
    np.putmask(temp_array, read_sub_array==s_nodata, s_nodata)

    with lock:
        # ===== LOCK HERE =====
        # Open target file handle
        t_fh = gdal.Open(target_dem_path, gdal.GA_Update)
        t_band = t_fh.GetRasterBand(1)

        # Sliced down chunk gets written into new file its original position in
        # the file (super array dimensions and offsets have been calculated,
        # used, and discarded and are no longer applicable)
        t_band.WriteArray(temp_array, x_start, y_start)

        t_band = None
        t_fh = None
        # ===== UNLOCK HERE =====

    # Explicit memory management
    read_array = None
    super_array = None
    new_data = None
    read_sub_array = None
    temp_array = None

def lock_init(l):
    '''
    Mini helper method that allows us to use a global lock accross a pool of
    processes. Used to safely read and write the input/output rasters.
    l:              mp.lock() created and passed as part of mp.pool
                    initialization
    '''
    global lock
    lock = l

def ParallelRCP(in_dem_path, out_dem_path, chunk_size, overlap, method,
                options, num_threads=1, verbose=False):
    '''
    Breaks a raster into smaller chunks for easier processing.
    in_dem_path:    Full path to input raster.
    out_dem_path:   Full path to resulting raster.
    chunk_size:     Square dimension of data chunk to process.
    overlap:        Data to be read beyond dimensions of chunk_size to ensure
                    methods that require neighboring pixels produce accurate
                    results on the borders. Should be at least 2x any filter
                    or kernel size for any method (will automattically be set
                    if method is blur_gauss, blur_mean, clahe, or TPI).
    method:         Name of the raster processing tool to be run on the chunks.
    options:        Dictionary of opt, value pairs to be passed to the
                    processing tool. Any opts that don't apply to the specific
                    method will be ignored.
    verbose:        Flag to print out more information (including mdenoise
                    output)

    Returns the time needed to process the entire raster.
    '''

    start = datetime.datetime.now()

    # Method name and option checks
    if method == "blur_gauss":
        gauss_opts = ["filter_size"]
        for opt in gauss_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))
        # Check overlap against filter_size
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]

    elif method == "mdenoise":
        mdenoise_opts = ["t", "n", "v"]
        for opt in mdenoise_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))

    elif method == "clahe":
        clahe_opts = ["filter_size", "clip_limit"]
        for opt in clahe_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]

    elif method == "TPI":
        TPI_opts = ["filter_size"]
        for opt in TPI_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]

    elif method == "blur_mean":
        mean_opts = ["filter_size"]
        for opt in mean_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]

    elif method == "hillshade":
        hillshade_opts = ["alt", "az"]
        for opt in hillshade_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))

    elif method == "skymodel":
        sky_opts = ["lum_file"]
        for opt in sky_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method \
                                 {}.".format(opt, method))

    else:
        raise NotImplementedError("Method not recognized: %s" %method)

    gdal.UseExceptions()

    # Get source file metadata (dimensions, driver, proj, cell size, nodata)
    print("Processing {0:s}...".format(in_dem_path))
    s_fh = gdal.Open(in_dem_path, gdal.GA_ReadOnly)
    rows = s_fh.RasterYSize
    cols = s_fh.RasterXSize
    driver = s_fh.GetDriver()
    s_band = s_fh.GetRasterBand(1)

    # Get source georeference info
    transform = s_fh.GetGeoTransform()
    projection = s_fh.GetProjection()
    cell_size = abs(transform[5])  # Assumes square pixels where height=width
    s_nodata = s_band.GetNoDataValue()

    if s_nodata is None:
        raise Exception("No NoData value set in input DEM.")
    if verbose:
        print("\tSource NoData Value: {0:f}\n".format(s_nodata))

    # Close source file handle
    s_band = None
    s_fh = None

    if verbose:
        print("Method: {}".format(method))
        print("Options:")
        for opt in options:
            print("\t{}: {}".format(opt, options[opt]))
        print("Preparing output file {}...".format(out_dem_path))
        print("\tOutput dimensions: {} rows by {} columns.".format(rows, cols))
        print("\tOutput size: {}".format(sizeof_fmt(rows * cols * 4)))
        print("\tOutput NoData Value: {}".format(s_nodata))

    # Set up target file in preparation for future writes
    # If we've been given a vrt as a source, force the output to be geotiff
    if driver.LongName == 'Virtual Raster':
        driver = gdal.GetDriverByName('gtiff')
    if os.path.exists(out_dem_path):
        raise IOError("Output file {} already exists.".format(out_dem_path))
    t_fh = driver.Create(out_dem_path, cols, rows, 1, gdal.GDT_Float32)
    t_fh.SetGeoTransform(transform)
    t_fh.SetProjection(projection)
    t_band = t_fh.GetRasterBand(1)
    t_band.SetNoDataValue(s_nodata)

    # Close target file handle (causes entire file to be written to disk)
    t_band = None
    t_fh = None

    if method == "skymodel":
        if verbose:
            print("Reading in luminance file {}".format(options["lum_file"]))
        lines = []
        with open(options["lum_file"], 'r') as l:
            reader = csv.reader(l)
            for line in reader:
                lines.append(line)
        options["lum_lines"] = lines


    # This check will parallelize the process assuming a file that is square or
    # fairly close to it. A file with one dimension that vastly exceeds the
    # limit while the other does not exceed it at all will attempt to be read
    # as one chunk. This could lead to out-of-memory issues.
    #
    # Also, we could probably code up an automatic chunk_size setter based on
    # data type and system memory limits
    if rows > chunk_size and cols > chunk_size:
        # calculate breaks every chunk_size pixels
        row_splits = list(range(0, rows, chunk_size))
        col_splits = list(range(0, cols, chunk_size))

        # add total number of rows/cols to be last break (used for x/y_end)
        row_splits.append(rows)
        col_splits.append(cols)

        # Double the overlap just to be safe. This distance becomes one side of
        # the super_array beyond the wanted data (f2 <> x values <> f2)
        f2 = 2 * overlap

        # Iterable variables sent to child processes in tuple
        iterables = []  # List of tuples to be iterated over with pool.map()
        total_chunks = (len(row_splits) - 1) * (len(col_splits) - 1)
        progress = 0

        # === Multiprocessing notes ===
        # Procedure: open s/t, get and set relevant metadata, close, create
        # list of chunk objects, create pool, execute super_array with
        # map(function, list of chunks)
        #   x/y_start = col/row_splits[j/i]- starting original raster index
        #   of the chunk
        #   x/y_end = col/row_splits[j/i +1]- ending (up to, not including)
        #   original raster index of the chunk

        # Create simple chunk objects that hold data about each chunk to be
        # sent to the processor
        # Rows = i = y values, cols = j = x values
        for i in range(0, len(row_splits) - 1):
            for j in range(0, len(col_splits) - 1):
                progress +=1

                # chunk object to hold all the data
                chunk = Chunk()

                # These are specific to each chunk
                chunk.progress = progress
                chunk.tile = "{}-{}".format(i, j)
                # x/y_start are the starting position of the original chunk
                # before adjusting the dimensions to read in the super array;
                # they are not used directly in the ReadAsArray() calls but are
                # used as the location that the altered array should be
                # written in the output bands WriteArray() calls.
                chunk.x_start = col_splits[j]
                chunk.y_start = row_splits[i]
                # end positions of initial chunk, used to compute read window
                chunk.x_end = col_splits[j+1]
                chunk.y_end = row_splits[i+1]

                # These are constant over the whole raster
                chunk.s_nodata = s_nodata
                chunk.cell_size = cell_size
                chunk.mdenoise_path = mdenoise_path
                chunk.in_dem_path = in_dem_path
                chunk.out_dem_path = out_dem_path
                chunk.f2 = f2
                chunk.rows = rows
                chunk.cols = cols
                chunk.total_chunks = total_chunks
                chunk.method = method
                chunk.options = options
                chunk.verbose = verbose
                chunk.start_time = start

                iterables.append(chunk)

        # Create lock to lock s_fh and t_fh reads and writes
        l = mp.Lock()

        # Call pool.map with the lock initializer method, super array
        # processor, and list of chunk objects.
        # chunksize=1 keeps the input processing more-or-less in order
        # (otherwise, for 4 processes working on 100 chunks, each process
        # starts at 0, 25, 50, and 75).
        # pool.map() guarantees the results will be in order, but not
        # necessarily the processing.
        with mp.Pool(processes=num_threads,
                     initializer=lock_init,
                     initargs=(l,)
                     ) as pool:
            pool.map(ProcessSuperArray, iterables, chunksize=1)

    # If it doesn't fit in one chunk, no need to chunk it up
    # TODO: finish this else path
    else:
        # sub_data = s_fh.ReadAsArray()
        # # Do something with the data
        # if method == "fftconvolve":
        #     new_data = blur(sub_data, options["filter_size"])
        # elif method == "mdenoise":
        #     new_data = mdenoise(sub_data, options["t"], options["n"], options["v"])
        # elif method == "hillshade":
        #     new_data = hillshade(super_array)
        # else:
        #     raise NotImplementedError("Method not implemented: %s" %method)
        # t_band.WriteArray(new_data)
        pass

    finish = datetime.datetime.now() - start
    if verbose:
        print(finish)
    return(finish)

def RCProcessing(in_dem_path, out_dem_path, chunk_size, overlap, method, options):
    '''
    --- OLD- SUPERSEDED BY ParalleRCP ---
    Breaks a raster into smaller chunks for easier processing.
    in_dem_path:    Full path to input raster.
    out_dem_path:   Full path to resulting raster.
    chunk_size:     Square dimension of data to be operated on.
    overlap:        Data to be read beyond dimensions of chunk_size to ensure
                    methods that require neighboring pixels produce accurate
                    results on the borders. Should be at least 2x any filter
                    or kernel size for any method (will automattically be set if
                    method is fftconvolve).
    method:         Name of the raster processing tool to be run on the chunks.
    options:        Dictionary of opt, value pairs to be passed to the
                    processing tool. Any opts that don't apply to the specific
                    method will be ignored.
    '''

    # Method name and option checks
    if method == "fftconvolve":
        fft_opts = ["filter_size"]
        for opt in fft_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method {}.".format(opt, method))
        # Check overlap against filter_size
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]
    elif method == "mdenoise":
        mdenoise_opts = ["t", "n", "v"]
        for opt in mdenoise_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method {}.".format(opt, method))
    elif method == "clahe":
        clahe_opts = ["filter_size", "clip_limit"]
        for opt in clahe_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method {}.".format(opt, method))
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]
    elif method == "TPI":
        TPI_opts = ["filter_size"]
        for opt in TPI_opts:
            if opt not in options:
                raise ValueError("Required option {} not provided for method {}.".format(opt, method))
        if overlap < 2 * options["filter_size"]:
            overlap = 2* options["filter_size"]
    else:
        raise NotImplementedError("Method not implemented: %s" %method)

    gdal.UseExceptions()

    start = datetime.datetime.now()
    #print("Started: " + start.strftime("%Y-%m-%d %H:%M:%S"))

    # Get source file
    print("Opening {0:s}...".format(in_dem_path))
    s_fh = gdal.Open(in_dem_path, gdal.GA_ReadOnly)
    rows = s_fh.RasterYSize
    cols = s_fh.RasterXSize
    driver = s_fh.GetDriver()
    s_band = s_fh.GetRasterBand(1)

    # Get source georeference info
    transform = s_fh.GetGeoTransform()
    projection = s_fh.GetProjection()
    # These are Global Variables, handle with care
    global cell_size
    global s_nodata
    cell_size = abs(transform[5]) # Assumes square pixels where height=width
    s_nodata = s_band.GetNoDataValue()

    if s_nodata is None:
        raise Exception("No NoData value set in input DEM.")
    print("Source NoData Value: {0:f}\n".format(s_nodata))

    # Set up target file
    t_fh = driver.Create(out_dem_path, cols, rows, 1, gdal.GDT_Float32)
    t_fh.SetGeoTransform(transform)
    t_fh.SetProjection(projection)
    t_band = t_fh.GetRasterBand(1)
    t_band.SetNoDataValue(s_nodata)

    # This check will limit the memory usage assuming a file that is square or
    # fairly close to it. A file with one dimension that vastly exceeds the size
    # limit while the other does not exceed it at all will attempt to be read
    # as one chunk. This could lead to out-of-memory issues.
    #
    # Also, we could probably code up an automatic chunk_size setter based on
    # data type and system memory limits
    if rows > chunk_size and cols > chunk_size:
        # calculate breaks every chunk_size pixels
        row_splits = list(range(0, rows, chunk_size))
        col_splits = list(range(0, cols, chunk_size))

        # add total number of rows/cols to be last break
        row_splits.append(rows)
        col_splits.append(cols)

        # Filter factor multiple- equals one side of super_array beyond wanted data
        f2 = 2 * overlap
        # print(row_splits)
        # print(col_splits)

        # Progress variables
        total_chunks = (len(row_splits) - 1) * (len(col_splits) - 1)
        progress = 0

        # === Multiprocessing notes ===
        # Variables/Objects needed below this point:
        #   s_fh: complex object, ReadAsArray() used to get source raster data
        #   t_band: complex objet, WriteArray() used to write new raster data
        #   row_splits, col_splits: lists, read only, define indices of the source raster to read from
        #   progress: int, r/w, progress counter
        #   total_chunks: int, read only, for progress counter
        #   s_nodata: float, read only, global variable
        #   options: dictionary, read only, passed through to processing methods
        #   temp_dir: string, read only, global used to create temp asc path for mdenoise
        #   cell_size: float, read only, global used to create temp asc
        #   mdenoise_path: string, read only, global
        #
        # Would need to handle data going to s_fh and t_band (file access) and to progress (simple variable)
        # All data shared between processes must be picklable. May need to open/close both s_fh and t_fh in each process


        # Rows = i = y values, cols = j = x values
        # row_splits[i] and col_splits[j] represent the starting position of the original chunk before the super array is read in; they are not used directly in the ReadAsArray() calls but are used as the location that the altered array should be written in the output bands WriteArray() calls.
        for i in range(0, len(row_splits) - 1):
            for j in range(0, len(col_splits) - 1):
                progress +=1

                # Non-edge-case values for super array
                x_size = col_splits[j+1] - col_splits[j] + 2 * f2
                y_size = row_splits[i+1] - row_splits[i] + 2 * f2
                x_off = col_splits[j] - f2
                y_off = row_splits[i] - f2

                # Values for ReadAsArray, these aren't changed later unelss the border case checks change them
                read_x_off = x_off
                read_y_off = y_off
                read_x_size = x_size
                read_y_size = y_size

                # Slice values (of super_array) for copying read_array in to super_array, these aren't changed later unelss the border case checks change them
                sa_x_start = 0
                sa_x_end = x_size
                sa_y_start = 0
                sa_y_end = y_size

                # Edge logic
                # If super_array exceeds bounds of image:
                #   Adjust x/y offset to appropriate place (for < 0 cases only).
                #   Reduce read size by 2*overlap (we're not reading that edge area on one side)
                #   Move start or end value for super_array slice by 2*overlap
                # Each of x/y can only be under (start of row/col) or over (end of row/col)
                #   ie, the input raster must be broken into at least two chunks in each direction
                # Checks both x and y, setting read and slice values for each dimension if needed
                if x_off < 0:
                    read_x_off = 0
                    read_x_size = x_size - f2
                    sa_x_start = f2
                elif x_off + x_size > cols:
                    read_x_size = x_size - f2
                    sa_x_end = -1 * f2

                if y_off < 0:
                    read_y_off = 0
                    read_y_size = y_size - f2
                    sa_y_start = f2
                elif y_off + y_size > rows:
                    read_y_size = y_size - f2
                    sa_y_end = -1 * f2

                percent = (progress / total_chunks) * 100
                print("Tile {0:d}-{1:d}: {2:d} of {3:d} ({4:0.2f}%)".format(i, j, progress, total_chunks, percent))

                #print("Read args: %d %d %d %d" %(read_x_off, read_y_off, read_x_size, read_y_size))

                # Master read call. read_ variables have been changed for edge cases if needed
                read_array = s_fh.ReadAsArray(read_x_off, read_y_off, read_x_size, read_y_size)
                # Arrays are of form [rows, cols], thus [y, x] when slicing

                # Array holding superset of actual desired window, initialized to NoData value
                # Edge case logic insures edges fill appropriate portion when loaded in
                # super_array must be of type float for fftconvolve
                super_array = np.full((y_size, x_size), s_nodata)

                # The cells of super_array correspondning to the read_array are replaced with data from read_array. This should change every value, except for edge cases that leave portions of the super_array as NoData
                #print("Super Array slices: [%d:%d, %d:%d]" %(sa_y_start, sa_y_end, sa_x_start, sa_x_end))
                super_array[sa_y_start:sa_y_end, sa_x_start:sa_x_end] = read_array

                # Do something with the data
                if method == "fftconvolve":
                    new_data = blur(super_array, options["filter_size"])
                elif method == "mdenoise":
                    new_data = mdenoise(super_array, options["t"], options["n"], options["v"])
                elif method == "hillshade":
                    new_data = hillshade(super_array)
                elif method == "clahe":
                    new_data = exposure.equalize_adapthist(super_array.astype(int), options["filter_size"], options["clip_limit"])
                elif method == "TPI":
                    new_data = TPI(super_array, options["filter_size"])
                else:
                    raise NotImplementedError("Method not implemented: %s" %method)

                # Resulting array is a superset of the data; we need to slice it down before writing it
                temp_array = new_data[f2:-1*f2, f2:-1*f2]

                # slice down super_array to get original chunk of data (ie, super_array minus additional data on edges) to use for finding NoData areas
                read_sub_array = super_array[f2:-f2, f2:-f2]

                # Reset NoData values in our result to match the NoData areas in the source array (areas in temp_array where corresponding cells in read_sub_array==NoData get set to s_nodata)
                np.putmask(temp_array, read_sub_array==s_nodata, s_nodata)

                # Sliced down chunk gets written into new file its proper position in the file (super array dimensions and offsets have been calculated, used, and discarded and are no longer applicable)
                t_band.WriteArray(temp_array, col_splits[j], row_splits[i])

    # If it fits in one chunk (see note above), read entire file at once
    # TODO: check for NoData stuff on this branch. Haven't kept up to date compared to "if" branch.
    else:
        sub_data = s_fh.ReadAsArray()
        # Do something with the data
        if method == "fftconvolve":
            new_data = blur(sub_data, options["filter_size"])
        elif method == "mdenoise":
            new_data = mdenoise(sub_data, options["t"], options["n"], options["v"])
        elif method == "hillshade":
            new_data = hillshade(super_array)
        else:
            raise NotImplementedError("Method not implemented: %s" %method)
        t_band.WriteArray(new_data)

    # === Multiprocessing Would End Here ===

    # Close out band first, then file handle (GDAL python gotcha)
    print("Closing output file {0:s}...".format(out_dem_path))
    t_band = None
    t_fh = None

    finish = datetime.datetime.now() - start
    print("Total time: " + str(finish))


# ==============================================================================
# Main Variables

# Global variables
# These are read in as part of opening the file in smooth() but will be used by WriteASC() as part of the mdenoise() call
# s_nodata is used several places; really needs to have been set in input DEM.
global cell_size
global s_nodata
global mdenoise_path
mdenoise_path = "c:\\temp\\gis\\lidar\\MDenoise.exe"

# Need this check for multiprocessing in windows
if "__main__" in __name__:

    #in_dem = "f:\\CacheValley_Lidar_2016\\ERDAS_IMG_Raster_DEM\\DEM-ft.tif"
    #smooth_dem = "f:\\CacheValley_Lidar_2016\\ERDAS_IMG_Raster_DEM\\DEM-ft-smoothed-80_60_80.tif"
    #hs_dem = "f:\\CacheValley_Lidar_2016\\ERDAS_IMG_Raster_DEM\\DEM-ft-smoothed-hs-80_60_80-z80.tif"

    #in_dem = "f:\\CacheValley_Lidar_2016\\ERDAS_IMG_Raster_DEM\\DEM-ft.tif"
    #smooth_dem = "e:\\lidar\\dem\\DEM-ft-md506050.tif"
    #s_dem = "e:\\lidar\\dem\\DEM-ft-80-90-90_hs.tif"

    #in_dem = "c:\\temp\\gis\\dem_state.tif"
    #smooth_dem = "c:\\temp\\gis\\dem_state_gauss30.tif"
    #hs_dem = "c:\\temp\\gis\\hstest\\dem_state_gauss30_sky.tif"
    lum = "c:\\temp\\gis\\skyshade\\lum\\1_45_315_150.csv"

    #in_dem = "e:\\lidar\\canyons\\dem\\merged_raw_dem.vrt"
    smooth_dem = "e:\\lidar\\canyons\\dem\\CCDEM-ft_md506050-lzw.tif"
    hs_dem = "e:\\lidar\\canyons\\dem\\CCDEM-ft_md506050_skymodel.tif"

    # md105060 = n=10, t=0.50, v=60

    filter_f = 30
    window_size = 1500
    n = 50
    t = 0.60
    v = 50
    clip = 0.1

    #RCProcessing(in_dem, smooth_dem, window_size, filter_f, "mdenoise", {"n":n, "t":t, "v":v})
    #ParallelRCP(in_dem, smooth_dem, window_size, filter_f, "mdenoise", {"n":n, "t":t, "v":v}, 3, False)
    #ParallelRCP(in_dem, smooth_dem, window_size, filter_f, "blur_gauss", {"filter_size":30}, 3, True)
    #ParallelRCP(in_dem, smooth_dem, window_size, filter_f, "TPI", {"filter_size":60}, num_threads=4, verbose=True)
    ParallelRCP(smooth_dem, hs_dem, 4000, filter_f, "skymodel", {"lum_file":lum}, num_threads=3, verbose=True)
    # times = {}
    # for i in range(1, 11, 1):
    #     smooth_dem = "c:\\temp\\gis\\dem_state_ParallelRCPTest_{}.tif".format(i)
    #     times[i] = ParallelRCP(in_dem, smooth_dem, window_size, filter_f, "mdenoise", {"n":n, "t":t, "v":v}, i)
    #
    # for run in times:
    #     report = "{} processes: {}".format(run, times[run])
    #     print(report)

    # CLAHE, interesting contrast stretcher... doesn't work here, but keep in back of mind
    # Might make a good transparent hillshade base
    #clahe_in = "c:\\temp\\gis\\lidar\\single_tests\\single-ft-md-506050.tif"
    #clahe_out = "c:\\temp\\gis\\lidar\\single_tests\\clahe\\506050_clahe01-180.tif"
    #smooth(clahe_in, clahe_out, window_size, "clahe", filter_f, t, n, v, clip)
    #smooth(smooth_dem, hs_dem, window_size, "hillshade", filter_f, t, n, v)

    # Somethings going weird with edge cases: edge case tiles to the top and left of non-case edge tiles are coming out zero, but everything below and to the right come out as nodata.
    # Solved post-edge case problem (was checking if offset was > row/col, rather than if offset + size > row/col). Still getting all 0s in pre-edge cases, and dem values seem to be shifted up and left (-x and -y) by some multiple of the filter size.
    # Fixed! was giving weird offsets to band write array method. Should just be the starting col and row for that chunk (t_band.WriteArray(temp_array, col_splits[j], row_splits[i]))

    # Window Sizes, all else constant:
    # 2000: mdenoise.exe fails
    # 1500: 8 chunks, total time:   3:12
    # 1000: 15 chunks, total time:  3:20
    # 500:  50 chunks, total time:  3:38
    # Total time increases as number of chunks increases, due to overhead of writing/reading temp files
