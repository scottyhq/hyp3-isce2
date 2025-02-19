import netrc
import os
import shutil
import subprocess
from pathlib import Path
from platform import system
from typing import Tuple

import isceobj
import numpy as np
from isceobj.Util.ImageUtil.ImageLib import loadImage
from osgeo import gdal

gdal.UseExceptions()

ESA_HOST = 'dataspace.copernicus.eu'


class GDALConfigManager:
    """Context manager for setting GDAL config options temporarily"""

    def __init__(self, **options):
        """
        Args:
            **options: GDAL Config `option=value` keyword arguments.
        """
        self.options = options.copy()
        self._previous_options = {}

    def __enter__(self):
        for key in self.options:
            self._previous_options[key] = gdal.GetConfigOption(key)

        for key, value in self.options.items():
            gdal.SetConfigOption(key, value)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for key, value in self._previous_options.items():
            gdal.SetConfigOption(key, value)


def get_esa_credentials() -> Tuple[str, str]:
    netrc_name = '_netrc' if system().lower() == 'windows' else '.netrc'
    netrc_file = Path.home() / netrc_name

    if "ESA_USERNAME" in os.environ and "ESA_PASSWORD" in os.environ:
        username = os.environ["ESA_USERNAME"]
        password = os.environ["ESA_PASSWORD"]
        return username, password

    if netrc_file.exists():
        netrc_credentials = netrc.netrc(netrc_file)
        if ESA_HOST in netrc_credentials.hosts:
            username = netrc_credentials.hosts[ESA_HOST][0]
            password = netrc_credentials.hosts[ESA_HOST][2]
            return username, password

    raise ValueError(
        "Please provide Copernicus Data Space Ecosystem (CDSE) credentials via the "
        "ESA_USERNAME and ESA_PASSWORD environment variables, or your netrc file."
    )


def utm_from_lon_lat(lon: float, lat: float) -> int:
    """Get the UTM zone EPSG code from a longitude and latitude.
    See https://en.wikipedia.org/wiki/Universal_Transverse_Mercator_coordinate_system
    for more details on UTM coordinate systems.

    Args:
        lon: Longitude
        lat: Latitude

    Returns:
        UTM zone EPSG code
    """
    hemisphere = 32600 if lat >= 0 else 32700
    zone = int(lon // 6 + 30) % 60 + 1
    return hemisphere + zone


def extent_from_geotransform(geotransform: tuple, x_size: int, y_size: int) -> tuple:
    """Get the extent and resolution of a GDAL dataset.

    Args:
        geotransform: GDAL geotransform.
        x_size: Number of pixels in the x direction.
        y_size: Number of pixels in the y direction.

    Returns:
        tuple: Extent of the dataset.
    """
    extent = (
        geotransform[0],
        geotransform[3],
        geotransform[0] + geotransform[1] * x_size,
        geotransform[3] + geotransform[5] * y_size,
    )
    return extent


def make_browse_image(input_tif: str, output_png: str) -> None:
    with GDALConfigManager(GDAL_PAM_ENABLED='NO'):
        stats = gdal.Info(input_tif, format='json', stats=True)['stac']['raster:bands'][0]['stats']
        gdal.Translate(destName=output_png,
                       srcDS=input_tif,
                       format='png',
                       outputType=gdal.GDT_Byte,
                       width=2048,
                       strict=True,
                       scaleParams=[[stats['minimum'], stats['maximum']]],
                       )


def oldest_granule_first(g1, g2):
    if g1[14:29] <= g2[14:29]:
        return g1, g2
    return g2, g1


def load_isce2_image(in_path) -> tuple[isceobj.Image, np.ndarray]:
    """ Read an ISCE2 image file and return the image object and array.

    Args:
        in_path: The path to the image to resample (not the xml).

    Returns:
        image_obj: The ISCE2 image object.
        array: The image as a numpy array.
    """
    image_obj, _, _ = loadImage(in_path)
    array = np.fromfile(in_path, image_obj.toNumpyDataType())
    return image_obj, array


def write_isce2_image(output_path, array=None, width=None, mode='read', data_type='FLOAT') -> None:
    """ Write an ISCE2 image file.

    Args:
        output_path: The path to the output image file.
        array: The array to write to the file.
        width: The width of the image.
        mode: The mode to open the image in.
        data_type: The data type of the image.
    """
    if array is not None:
        array.tofile(output_path)
        width = array.shape[1]
    elif width is None:
        raise ValueError('Either a width or an input array must be provided')

    out_obj = isceobj.createImage()
    out_obj.initImage(output_path, mode, width, data_type)
    out_obj.renderHdr()


def get_geotransform_from_dataset(dataset: isceobj.Image) -> tuple:
    """Get the geotransform from an ISCE2 image object.

    Args:
        dataset: The ISCE2 image object to get the geotransform from.

    Returns:
        tuple: The geotransform in GDAL Format: (startLon, deltaLon, 0, startLat, 0, deltaLat)
    """
    startLat = dataset.coord2.coordStart
    deltaLat = dataset.coord2.coordDelta
    startLon = dataset.coord1.coordStart
    deltaLon = dataset.coord1.coordDelta

    return (startLon, deltaLon, 0, startLat, 0, deltaLat)


def resample_to_radar(
    mask: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    geotransform: tuple,
    data_type: type,
    outshape: tuple[int, int]
) -> np.ndarray:
    """Resample a geographic image to radar coordinates using a nearest neighbor method.
    The latin and lonin images are used to map from geographic to radar coordinates.

    Args:
        mask: The array of the image to resample
        lat: The latitude array
        lon: The longitude array
        geotransform: The geotransform of the image to resample
        data_type: The data type of the image to resample
        outshape: The shape of the output image

    Returns:
        resampled_image: The resampled image array
    """

    start_lon, delta_lon, start_lat, delta_lat = geotransform[0], geotransform[1], geotransform[3], geotransform[5]

    lati = np.clip((((lat - start_lat) / delta_lat) + 0.5).astype(int), 0, mask.shape[0] - 1)
    loni = np.clip((((lon - start_lon) / delta_lon) + 0.5).astype(int), 0, mask.shape[1] - 1)
    resampled_image = (mask[lati, loni]).astype(data_type)
    resampled_image = np.reshape(resampled_image, outshape)
    return resampled_image


def resample_to_radar_io(image_to_resample: str, latin: str, lonin: str, output: str) -> None:
    """Resample a geographic image to radar coordinates using a nearest neighbor method.
    The latin and lonin images are used to map from geographic to radar coordinates.

    Args:
        image_to_resample: The path to the image to resample
        latin: The path to the latitude image
        lonin: The path to the longitude image
        output: The path to the output image
    """
    maskim, mask = load_isce2_image(image_to_resample)
    latim, lat = load_isce2_image(latin)
    _, lon = load_isce2_image(lonin)
    mask = np.reshape(mask, [maskim.coord2.coordSize, maskim.coord1.coordSize])
    geotransform = get_geotransform_from_dataset(maskim)
    cropped = resample_to_radar(mask=mask,
                                lat=lat,
                                lon=lon,
                                geotransform=geotransform,
                                data_type=maskim.toNumpyDataType(),
                                outshape=(latim.coord2.coordSize, latim.coord1.coordSize)
                                )

    write_isce2_image(output, array=cropped, data_type=maskim.dataType)


def isce2_copy(in_path: str, out_path: str):
    """Copy an ISCE2 image file and its metadata.

    Args:
        in_path: The path to the input image file (not the xml).
        out_path: The path to the output image file (not the xml).
    """
    image, _, _ = loadImage(in_path)
    clone = image.clone('write')
    clone.setFilename(out_path)
    clone.renderHdr()
    shutil.copy(in_path, out_path)


def image_math(image_a_path: str, image_b_path: str, out_path: str, expression: str):
    """Run ISCE2's imageMath.py on two images.

    Args:
        image_a_path: The path to the first image (not the xml).
        image_b_path: The path to the second image (not the xml).
        out_path: The path to the output image.
        expression: The expression to pass to imageMath.py.
    """
    cmd = ['imageMath.py', '-e', expression, f'--a={image_a_path}', f'--b={image_b_path}', '-o', out_path]
    subprocess.run(cmd, check=True)
