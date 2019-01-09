# -*- coding: utf-8 -*-
"""
Raster and spatial interpolation functions.
"""
from time import time
from multiprocessing import Pool
import pandas as pd
import numpy as np
import fiona
import xarray as xr
from scipy.interpolate import griddata, Rbf, RectBivariateSpline
from pycrsx.utils import convert_crs
from pyproj import Proj, transform
from scipy.ndimage import map_coordinates
from util import grid_xy_to_map_coords, point_xy_to_map_coords, map_coords_to_xy


def process_grid_input(input_data, time_name, x_name, y_name, data_name, input_digits):
    if isinstance(input_data, pd.DataFrame):
        grouped = input_data.set_index([time_name, y_name, x_name])[data_name].sort_index()
        input_data = grouped.to_xarray().to_dataset()
    if isinstance(input_data, xr.Dataset):
        da1 = input_data[data_name]
        da2 = da1.transpose(time_name, y_name, x_name).sortby([time_name, y_name, x_name])
        arr1 = da2.values
        np.nan_to_num(arr1, False)
        x = da1[x_name].values
        y = da1[y_name].values
        time1 = da1[time_name].values
        xy_orig_pts = np.dstack(np.meshgrid(y, x)).reshape(-1, 2)

    return arr1, xy_orig_pts, time1


def process_point_input(df, time_name, x_name, y_name, data_name, input_digits):
    xy1 = list(zip(df[x_name], df[y_name]))
    xy_new1 = list(zip(*[transform(from_crs1, to_crs1, x, y) for x, y in xy1]))
    time = pd.to_datetime(df2[time_col].sort_values().unique())


def grid_to_grid(grid, time_name, x_name, y_name, data_name, grid_res, from_crs, to_crs=None, bbox=None, order=3, extrapolation='constant', cval=np.nan, digits=2, min_val=None):
    """
    Function to interpolate regularly or irregularly spaced values over many time stamps. Each time stamp of spatial values are interpolated independently (2D interpolation as opposed to 3D interpolation). The values can be aggregated in time, but but are not interpolated. Returns a DataFrame of gridded interpolated results at each time stamp or an xarray Dataset with the 3 dimensions.

    Parameters
    ----------
    grid : DataFrame or Dataset
        A pandas DataFrame or an xarray Dataset. It's recommended to use an xarray Dataset for the input grid as it ensures that the user knows that it is truly regular. Regardless, the input will be regularised.
    time_name : str
        If grid is a DataFrame, then time_name is the time column name. If grid is a Dataset, then time_name is the time coordinate name.
    x_name : str
        If grid is a DataFrame, then x_name is the x column name. If grid is a Dataset, then x_name is the x coordinate name.
    y_name : str
        If grid is a DataFrame, then y_name is the y column name. If grid is a Dataset, then y_name is the y coordinate name.
    data_name : str
        If grid is a DataFrame, then data_name is the data column name. If grid is a Dataset, then data_name is the data variable name.
    grid_res : int or float
        The resulting grid resolution in the unit of the final projection (usually meters or decimal degrees).
    from_crs : int or str or None
        The projection info for the input data if the result should be reprojected to the to_crs projection (either a proj4 str or epsg int).
    to_crs : int or str or None
        The projection for the output data similar to from_crs.
    bbox : tuple of int or float
        The bounding box for the output interpolation in the to_crs projection). None will return a similar grid extent as the input. The tuple should contain four ints or floats in the following order: (x_min, x_max, y_min, y_max)
    order : int
        The order of the spline interpolation, default is 3. The order has to be in the range 0-5. An order of 1 is linear interpolation.
    extrapolation : str
        The equivalent of 'mode' in the map_coordinates function. Options are: 'constant', 'nearest', 'reflect', 'mirror', and 'wrap'. Most reseaonable options for this function will be either 'constant' or 'nearest'. See `<https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.map_coordinates.html>`_ for more details.
    cval : int or float
        If 'constant' if passed to the extrapolation parameter, cval assigns the value outside of the boundary. Defaults to numpy.nan.
    digits : int
        The number of digits to round the output.

    Returns
    -------
    xarray Dataset
    """
    print('Preparing input and output')
    if from_crs == 4326:
        input_digits = 4
    else:
        input_digits = 0
    if (to_crs == 4326) | ((from_crs == 4326) & (to_crs is None)):
        output_digits = 4
    else:
        output_digits = 0

    ### Prepare input data
    arr1, xy_orig_pts, time1 = process_grid_input(grid, time_name, x_name, y_name, data_name, input_digits)

    input_coords, dxy, x_min, y_min = grid_xy_to_map_coords(xy_orig_pts, input_digits)

    ### convert to new projection and prepare X/Y data
    if isinstance(from_crs, (str, int)) & isinstance(to_crs, (str, int)):
        from_crs1 = Proj(convert_crs(from_crs, pass_str=True), preserve_units=True)
        to_crs1 = Proj(convert_crs(to_crs, pass_str=True), preserve_units=True)
        xy_new = np.array(list(zip(*[reversed(transform(from_crs1, to_crs1, x, y)) for y, x in xy_orig_pts]))).round(output_digits)
        out_y_min, out_x_min = xy_new.min(1)
        out_y_max, out_x_max = xy_new.max(1)
    else:
        out_y_max, out_x_max = xy_orig_pts.max(0)
        out_x_min = x_min
        out_y_min = y_min

    ### Prepare output data
    if isinstance(bbox, tuple):
        out_x_min, out_x_max, out_y_min, out_y_max = bbox

    new_x = np.arange(out_x_min, out_x_max, grid_res)
    new_y = np.arange(out_y_min, out_y_max, grid_res)

    xy_out = np.dstack(np.meshgrid(new_y, new_x)).reshape(-1, 2)

    if isinstance(from_crs, (str, int)) & isinstance(to_crs, (str, int)):
        xy_new_index = np.array(list(zip(*[reversed(transform(to_crs1, from_crs1, x, y)) for y, x in xy_out]))).T
    else:
        xy_new_index = xy_out

    output_coords = point_xy_to_map_coords(xy_new_index, dxy, x_min, y_min, float)

    ### Run interpolation (need to add mutliprossessing)
    arr2 = np.zeros((len(time1), output_coords.shape[1]), arr1.dtype)

    print('Running interpolations...')
    ## An example for using RectBivariateSpline as the equivalent to map_coordinates output (about half as fast):
#    arr2a = arr2.copy()
#
#    x_out = xy_new_index.T[1]
#    y_out = xy_new_index.T[0]
#
#    for d in np.arange(len(arr1)):
#        arr2a[d] = RectBivariateSpline(y, x, arr1[d], kx=3, ky=3).ev(y_out, x_out)

#    start1 = time()
    for d in np.arange(len(arr1)):
         map_coordinates(arr1[d], output_coords, arr2[d], order=order, mode=extrapolation, cval=cval, prefilter=True)
#    end1 = time()
#    setb = end1 - start1

    ### Reshape and package data
    print('Packaging up the output')
    arr2 = arr2.reshape((len(time1), len(new_x), len(new_y))).round(digits)

    if isinstance(min_val, (int, float)):
        arr2[arr2 < min_val] = min_val

    new_ds = xr.DataArray(arr2, coords=[time1, new_x, new_y], dims=['time', 'x', 'y'], name=data_name).to_dataset()

    return new_ds


def points_to_points(df, time_name, x_name, y_name, data_name, point_path, from_crs, to_crs=None, method='cubic', digits=2, min_val=None):
    """
    Function to take a dataframe of z values and interate through and resample both in time and space. Returns a DataFrame in the shape of the points from the point_shp.

    Parameters
    ----------
    df : DataFrame
        DataFrame containing four columns as shown in the below parameters.
    time_col : str
        The time column name.
    x_col : str
        The x column name.
    y_col : str
        The y column name.
    data_col : str
        The data column name.
    point_path : str
        Path to geometry file of points to be interpolated (e.g. shapefile). Can be anything that fiona/gdal can open.
    point_site_col : str
        The column name of the site names/numbers of the point_shp.
    grid_res : int
        The resulting grid resolution in meters (or the unit of the final projection).
    from_crs : int or str
        The projection info for the input data if the result should be reprojected to the to_crs projection (either a proj4 str or epsg int).
    to_crs : int or str
        The projection for the output data similar to from_crs.
    method : str
        The scipy griddata interpolation method to be applied (see `https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.griddata.html`_).
    digits : int
        The number of digits to round the results.

    Returns
    -------
    DataFrame
    """

    ### Read in points
    if isinstance(point_path, str):
        with fiona.open(point_path) as f1:
            point_crs = Proj(f1.crs, preserve_units=True)
            points = np.array([p['geometry']['coordinates'] for p in f1 if p['geometry']['type'] == 'Point'])
    else:
        raise ValueError('point_path must be a str path to a geometry file (e.g. shapefile)')

    ### Create the grids
    df2 = df.copy()

    time1 = pd.to_datetime(df2[time_name].sort_values().unique())

    ### Convert input data to crs of points shp and create input xy
    if to_crs is not None:
        to_crs1 = Proj(convert_crs(to_crs, pass_str=True), preserve_units=True)
        points = np.array([transform(point_crs, to_crs1, x, y) for x, y in points])
    else:
        to_crs1 = point_crs
    from_crs1 = Proj(convert_crs(from_crs, pass_str=True), preserve_units=True)
    xy1 = list(zip(df2[x_name], df2[y_name]))
    xy_new1 = list(zip(*[transform(from_crs1, to_crs1, x, y) for x, y in xy1]))
    df2[x_name] = xy_new1[0]
    df2[y_name] = xy_new1[1]

    ### Prepare the x and y of the points geodataframe output
    new_lst = []
    for name, group in df2.groupby(time_name):
        print(name)
        xy = group[[x_name, y_name]].values
        new_z = griddata(xy, group[data_name].values, points, method=method).round(digits)
        if isinstance(min_val, (int, float)):
            new_z[new_z < min_val] = min_val
        new_lst.extend(new_z.tolist())

    ### Create new df
    time_ar = np.repeat(time1, len(points))
    x_ar = np.tile(points.T[0], len(time1))
    y_ar = np.tile(points.T[1], len(time1))
    new_df = pd.DataFrame({'time': time_ar, 'x': x_ar, 'y': y_ar, data_name: new_lst}).set_index(['time', 'x', 'y'])

    ### Export results
    return new_df

