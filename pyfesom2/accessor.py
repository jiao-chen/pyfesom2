"""A Xarray accessor for unstructured-FESOM datasets.

The module aims to provide, currently unsupported, Xarray methods for unstructured-FESOM data. Current priority is to
have functionality to select and plot unstructured-FESOM data with same ease as Xarray's methods.This functionality is
provided through `pyfesom2` accessor (available on importing pyfesom2) and through helper functions that may
be used independently of the accessor. Because the intended use of accessor is on a well defined data structure of
FESOM2 data -- unlike Xarray which intends to support a generic dataset --, it provides an opportunity for additional
features and conveniences like: selecting using polygons, interactive plots, that are not part of default Xarray,
 for benefit of pyfesom2's users.

The methods implemented here differ with default Xarray's methods mostly from how longitudes and latitudes are
represented in an unstructured grid. Following sections briefly describe these differences  and design considerations
for pyfesom2 accessor.

Selections
----------
Xarray's sel method provides label based selection  on data object's dimensions using their values. For rectilinear
grids, the variables, longitude and latitude that define the grid would be in the dimensions (say lon, lat). This
provides a convenient way to select data in lon-lat space by using them as arguments in sel method, either as sequence
of  values (e.g, array)  for point selection or slices for rectangular region selection. In case of unstructured-FESOM
data, lat and lon are not dimensions of data as they are not orthogonal to each other and hence cannot be used as
indexers directly in `sel` method. They are instead  provided as additional coordinates (in parlance of Xarray and
NetCDF) on a common dimension `nod2` of the dataset.

Although lon and lat are not in data dimensions, the selections methods of this module allow using them as indexers
(or arguments) to selection methods to retain the convenience of Xarray's sel method, with a inherent data structure
limitation that they have to be of same size. To support more complex selections and to facilitate interaction with
interactive plots additional arguments like region, path  are introduced that take Shapely's geometries.

To mark these differences in method arguments clearly with respect to Xarray's sel method, the accessor's
selection methods ar named differently  with prefix `select`. The aspects of data selection that concern other
orthogonal dimensions (like time, level) rely on Xarray's sel method.

The sel method of Xarray uses cartesian distances (indirectly) on values of dimensions to select data. While this metric
in lon, lat  is representative of  a geodesic distance in a rectilinear grid, this cannot be assumed for unstructured
grids, as lon, lat are not orthogonal. To overcome this we project lon, lat onto a geo-centric frame of reference. The
Euclidean distance metric in this frame of reference is equivalent to a geodesic tunnel distance. This projection, while
can be more more memory intensive was found to be computationally acceptable even for large FESOM grids. Moreover,
selections on rectilinear grids can be done independently on each dimension using their (often sorted) 1 dimensional
values to find indices using standard and efficient algorithms. This is not possible in an unstructured grid as lon,lat
are not orthogonal and any selection in that space has to use both values at once. To address this, a KD-tree (from
Scipy) is used on, above described, projected lon and lat. This implementation was found to be reasonably efficient and
stable (also dependency wise) implementation for selecting multiple points for most FESOM grids.

A peculiarity of unstructured grid selection, specifically region selection, is necessity to retain face information
(containing triangulation information) for underlying grid without which data subsets have limited utility, especially
for spatial plotting. The `faces` coordinate variable contains indices of nodes that define grid faces (triangulation)
and these values are dimensioned (nelem, three) independently to a dataset's dimensions, they are hence not
automatically selected by lon, lat or nod2 indexers. Returning a valid face information on region selection would mean
re-evaluating the index values in faces variable based on new indices of nod2 that represent selected lon and lat.
Such a requirement to additionally return re-indexed faces is unlike regular selection in rectilinear grids.

Selections in pyfesom2 accessor mainly concerns lat-lon, other indexers such as time are passed to Xarray sel method.

Accessor
--------
Xarray provides mechanisms to extend functionality for datasets and data-arrays representing variables. Functionality
such as plotting are most intuitive on data-arrays of data variable while selections are intuitive on both datasets and
data-arrays. For spatial plotting on an unstructured triangular grids it is necessary to have face information
(triangulation) and data arrays cannot hold such information as they do not share dimensions with data array. To
facilitate spatial plotting methods on data-arrays it is hence necessary to provide over-lying dataset context that
contains such face information. This issue is also present for regional selections on data-arrays where faces from
context dataset are necessary. To facilitate sharing such dataset context, the accessor is implemented on a dataset
and data-array is wrapped in a Python class object. This has additional (opinionated) advantage of simplifying accessor
usage pattern to `dataset.pyfesom2.method()` for methods applicable to to entire dataset and
`dataset.pyfesom2.variable.method()` for methods on data-arrays.

"""
import warnings
from typing import Optional, Sequence, Union, MutableMapping, Tuple

import cartopy.crs as ccrs
import numpy as np
import xarray as xr
from shapely.geometry import MultiPolygon, Polygon, LineString

# New Types
BoundingBox = Sequence[float]
Region = Union[BoundingBox, Polygon]
MultiRegion = Union[Sequence[Polygon], MultiPolygon]
ArrayLike = Union[Sequence[float], np.ndarray, xr.DataArray]
Path = Union[LineString, Tuple[ArrayLike, ArrayLike]]


# Selection

# ---Utilities for selection

def distance_along_trajectory(lons: ArrayLike, lats: ArrayLike) -> ArrayLike:
    """Returns geodesic distance along a trajectory of lons and lons.

    Computes cumulative distance from starting lon, lat till end of array.

    Parameters
    ----------
    lons
        Array-like longitude values.
    lats
        Array-like latitude values.

    Returns
    -------
    ArrayLike
        Returns array containing distances in meters
    """
    from cartopy.geodesic import Geodesic
    geod = Geodesic()
    lons, lats = np.array(lons, ndmin=1, copy=False), np.array(lats, ndmin=1, copy=False)

    if np.ndim(lons) > 2:
        raise NotImplementedError('More then 2 dims in lons are currenty not supported')

    dists = np.zeros(lons.shape)

    if np.ndim(lons) == 1:
        points = np.c_[lons, lats]
        temp_dist = geod.inverse(points[0:-1], points[1:])[:, 0]
        dists[1:] = np.cumsum(temp_dist)
    else:
        for i, (_lons, _lats) in enumerate(zip(lons, lats)):
            points = np.c_[_lons, _lats]
            temp_dist = geod.inverse(points[0:-1], points[1:])[:, 0]
            dists[i, 1:] = np.cumsum(temp_dist)

    return dists


def normalize_distance(distance_array_in_m: ArrayLike) -> Tuple[str, ArrayLike]:
    """Returns best representation for distances in m or km and values.

    Parameters
    ----------
    distance_array_in_m

    Returns
    -------
    tuple
        Returns tuple containing best units in m or km and transformed values.
    """
    distance_array_in_km = distance_array_in_m / 1000.0
    len_array = distance_array_in_m.shape[0]
    # if more then 1/3 of points are best suited to be expressed in m else in km
    if np.count_nonzero(distance_array_in_km < 1) > len_array // 3:
        return "m", distance_array_in_m
    else:
        return "km", distance_array_in_km


class SimpleMesh:
    """Wrapper that fakes pyfesom's mesh object for purposes of this module"""

    def __init__(self, lon: ArrayLike, lat: ArrayLike, faces: ArrayLike):
        self.x2 = lon
        self.y2 = lat
        self.elem = faces


# ---Selection functions

def select_bbox(xr_obj: Union[xr.DataArray, xr.Dataset],
                bbox: BoundingBox,
                coords_dataset: Optional[xr.Dataset] = None) -> xr.Dataset:
    """Returns subset Dataset or DataArray for bounding box.

    This method uses triangulation indices in faces (as argument or as coordinate in a dataset) to select nodes
    belonging to faces in bounding box. Hence, nodes that belong to faces entirely contained in bounding box are
    returned. A Xarray dataset is returned regardless of input type to retain face coordinate information in the subset.
    Returned values of faces in returned subset correspond to triangulation using new indices of nodes. This method
    uses basic numpy's capabilities and does not depend on Shapely like other selection methods.

    Parameters
    ----------
    xr_obj
        Xarray's Dataset or DaraArray, for DataArrays faces argument is necessary.
    bbox
        Bounding box can be specified as as sequence of size 4 (lists or tuple or array) containing bounds
        from lower-left to upper-right of longitudes and latitudes. For instance: (xmin, ymin, xmax, ymax).
    coords_dataset
        if xr_obj is a fesom2 Dataset, this argument is not necessary.
        if xr_obj is a xr.DataArray: all metadata needed for selection cannot be often contained in a dataarray,
        this can be provided as a (coordinate) dataset containing necessary additional coordinate information
        using this argument.
    Returns
    -------

    """
    from .ut import cut_region

    if isinstance(xr_obj, xr.Dataset):
        lats, lons = xr_obj.lat, xr_obj.lon
        faces = xr_obj.faces
        ret = xr_obj
    elif isinstance(xr_obj, xr.DataArray):
        if coords_dataset is None:
            raise ValueError(f"Selection on a dataarray needs coords_dataset argument containing lon,lat, faces as"
                             f" coordinates.")
        coords_dataset = coords_dataset[['lon', 'lat', 'faces']] # in case it is not just get needed coords and dims
        lats, lons = coords_dataset.lat, coords_dataset.lon
        faces = coords_dataset.faces
        ret = xr.merge([xr_obj, coords_dataset])

    mesh = SimpleMesh(lons, lats, faces)
    bbox = np.asarray(bbox)
    # cut region takes xmin, xmax, ymin, ymax
    cut_faces, cut_indices = cut_region(mesh, [bbox[0], bbox[2], bbox[1], bbox[3]])
    cut_faces = np.asarray(cut_faces)
    uniq, inv_index = np.unique(cut_faces.ravel(), return_inverse=True)
    new_faces = inv_index.reshape(cut_faces.shape)
    ret = ret.isel(nod2=uniq, nelem=cut_indices)
    ret['faces'] = (('nelem', 'three'), new_faces)
    return ret


def select_region(xr_obj: Union[xr.DataArray, xr.Dataset], region: Region,
                  coords_dataset: Optional[xr.DataArray] = None) -> xr.Dataset:
    """Returns a FESOM data subset for specified arbitrary region.

    This method uses vectorized Shapely's vectorized routines to find nodes contained by specified polygon. Faces
    (or triangles) have all the selected nodes are re-indexed to indices of selected nodes. To retain this triangulation
    information a Dataset is returned.

    Parameters
    ----------
    xr_obj
        Xarray's Dataset or DataArray. In case of DataArray, faces argument is required.
    region
        As as length 4 sequence or Shapely's polygon geometries like Polygon or box.
    faces
        Array-like with 2 dims with last dimension of size 3 containing triangulation (indices).
        This is required for DataArrays, Datasets are probed for coordinate variable named faces.

    Returns
    -------
    xr.DataSet
    """
    from shapely.geometry import box, Polygon
    from shapely.prepared import prep
    from shapely.vectorized import contains as vectorized_contains

    if isinstance(region, Sequence) and len(region) == 4:
        region = box(*region)
    elif isinstance(region, Polygon):
        region = region
    else:
        raise ValueError(f"Supplied region data can be a sequence of (minlon, minlat, maxlon, maxlat) or "
                         f"a Shapely's Polygon. This {region} is not supported.")

    if isinstance(xr_obj, xr.Dataset):
        lats, lons = xr_obj.lat, xr_obj.lon
        faces = np.asarray(xr_obj.faces)
        nelem = np.asarray(xr_obj.nelem)
        ret = xr_obj
    elif isinstance(xr_obj, xr.DataArray):
        if coords_dataset is None:
            raise ValueError(f"Selection on a dataarray needs coords_dataset argument containing lon,lat, faces as"
                             f" coordinates.")
        coords_dataset = coords_dataset[['lon', 'lat', 'faces']] # in case it is not just get needed coords and dims
        lats, lons = coords_dataset.lat, coords_dataset.lon
        faces = np.asarray(coords_dataset.faces)
        nelem = np.asarray(coords_dataset.nelem)
        ret = xr.merge([xr_obj, coords_dataset])

    # buffer is necessry to facilitte floating point comparisions
    # buffer can be thought as tolerance around region in degrees
    # its value should be at least precision of data type of lats, lons (np.finfo)
    region = region.buffer(1e-6)
    prep_region = prep(region)
    selection = vectorized_contains(prep_region, np.asarray(lons), np.asarray(lats))
    if np.count_nonzero(selection) == 0:
        warnings.warn('No points in domain are within region, returning original data.')
        return xr_obj

    selection = selection[faces]
    face_mask = np.all(selection, axis=1)
    cut_faces = faces[face_mask]
    cut_faces = np.asarray(cut_faces)
    cut_indices = nelem[face_mask]
    uniq, inv_index = np.unique(cut_faces.ravel(), return_inverse=True)
    new_faces = inv_index.reshape(cut_faces.shape)
    ret = ret.isel(nod2=uniq, nelem=cut_indices)

    if 'faces' in ret.coords:
        ret = ret.drop_vars('faces')

    if len(uniq) == 0:
        warnings.warn("No found points for the region are contained in dataset's triangulation (faces), "
                      "returning object without faces.")
        return ret  # no faces in coords
    ret['faces'] = (('nelem', 'three'), new_faces)
    return ret


def select_points(xr_obj: Union[xr.Dataset, xr.DataArray],
                  lon: ArrayLike, lat: ArrayLike, method: str = 'nearest', tolerance: Optional[float] = None,
                  tree: Optional[object] = None, return_distance: Optional[bool] = True,
                  selection_dim_name: Optional[str] = "nod2", **other_dims) -> Union[xr.Dataset, xr.DataArray]:
    """Returns a FESOM point dataset for specified longitudes and latitudes and other dimension representing
     a trajectory.

    This method selects points based on geodesic distance  to specified  lon, lat (and optionally to specified
    other dimensions). All arguments have to be of same shape and size. To select points geodesic-ally
    closest to input FESOM grid, both longitudes and latitudes of FESOM grid and and desired destination-transect points
    (arguments lon, lat) are projected onto geocentric coordinates.  This means tunnel distance as geedesic metric.
    A KDtree is used to efficiently select multiple points. For other orthogonal dimensions, default label based
    indexing of Xarray's sel method is used.

    Note
    ----
    This is unlike default Xarray's selection for rectilinear grids on longitudes, latitudes where cartesian distances
    are not used.

    Parameters
    ----------
    xr_obj
        xr.Dataset or DataArray.
    lon
        Array-like longitudes.
    lat
        Array-like latitudes.
    method
        "nearest" (or geodesic-ally closest)  is currently supported.
    tolerance
        A tolerance radius to select non missing values, currently not supported.
    tree
        A Scipy cKDtree object, this speeds up repeated queries on input data.
    return_distance
        If True returns distance along selection lon, lat in metric units as a coordinate of returned dataset.
    selection_dim_name
        When points are defined on more then lon and lat, this argument defines the name of stacked dimension. By
        default data is stacked on dimension nod2.
    other_dims
        Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
        have to be dimensions of dataarray or dataset.

    Returns
    -------
    xr.Dataset or xr.DataArray
        Returns data type similar to input data.
    """
    from cartopy.crs import Geocentric, Geodetic
    from scipy.spatial import cKDTree
    src_lons, src_lats = np.asarray(xr_obj.lon), np.asarray(xr_obj.lat)

    set_len_dims = {np.size(lon), np.size(lat), *[np.size(val) for val in other_dims.values()]}

    if len(set_len_dims) > 1:
        raise ValueError('For point selection length of all supplied dims args should be same.')

    if not method == 'nearest':
        raise NotImplementedError("Spatial selection currently supports only nearest neighbor lookup")
    geocentric_crs, geodetic_crs = Geocentric(), Geodetic()
    if tree is None:
        src_pts = geocentric_crs.transform_points(geodetic_crs, src_lons, src_lats)
        tree = cKDTree(src_pts, leafsize=32, compact_nodes=False, balanced_tree=False)

    if isinstance(lon, xr.DataArray) and isinstance(lat, xr.DataArray):
        sel_dim = tuple(lon.dims)
    else:
        sel_dim = selection_dim_name

    dst_pts = geocentric_crs.transform_points(geodetic_crs, np.asarray(lon), np.asarray(lat))

    if tolerance is None:
        _, ind = tree.query(dst_pts)
    else:
        raise NotImplementedError('tolerance is currently not supported.')

    other_dims = {k: xr.DataArray(np.asarray(v), dims=sel_dim) for k, v in other_dims.items()}
    ret_obj = xr_obj.isel(nod2=xr.DataArray(ind, dims=sel_dim)).sel(**other_dims, method=method)

    # from faces, which will not be useful in returned dataset
    # unless we reindex them, but is there a use case for that?
    if 'faces' in ret_obj.coords:
        ret_obj = ret_obj.drop_vars('faces')
    if return_distance:
        dist = distance_along_trajectory(lon, lat)
        dist_units, dist = normalize_distance(dist)
        ret_obj = ret_obj.assign_coords({'distance': (sel_dim, dist)})
        ret_obj.distance.attrs['units'] = dist_units
        ret_obj.distance.attrs['long_name'] = f"distance along trajectory"
    return ret_obj


def select(xr_obj: xr.Dataset, method: str = 'nearest',
           tolerance: float = None, region: Optional[Region] = None,
           path: Optional[Union[Path, MutableMapping]] = None, tree: Optional[object] = None,
           **indexers) -> Union[xr.Dataset, xr.DataArray]:
    """A generalized interface to select data from unstructured FESOM dataset.

    This method provides interface to similar to sel method of Xarray for an unstructured FESOM data. In addition there
    are additional arguments to select polygons and paths specified as Shapely's geometries. This method wraps
    select_region and select_points methods of this pyfesom2.accessor module.


    Parameters
    ----------
    xr_obj
        xr.Dataset. Dataset must contain faces as coordinate variable for region based selection.
    method
        "nearest" (or geodesic-ally closest)  is currently supported.
    tolerance
        A tolerance radius to select non missing values, currently not supported.
    region
        As as length 4 sequence or Shapely's polygon geometries like Polygon or box.
    path
        A tuple of same-sized longitudes, latitudes or Shapely's LineString or a dictionary with keys as dimensions.
    tree
        A Scipy cKDtree object, this speeds up repeated queries on input data.
    indexers
        Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
        have to be dimensions of the dataset. These indexers are passed to xarray's sel method as-is.

    Returns
    -------
    xr.Dataset
    """
    lat = indexers.pop('lat', None)
    lon = indexers.pop('lon', None)
    lat_indexer = True if lat is not None else False
    lon_indexer = True if lon is not None else False

    if (lat_indexer or lon_indexer) and (region is not None or path is not None):
        # TODO: do this combinations better, doesn't check if path and region are both given
        raise ValueError("Only one option: lat, lon as indexer or path or region is supported")

    ret_arr = xr_obj

    if lat_indexer or lon_indexer:
        if lat_indexer and lon_indexer:
            if method == 'nearest':
                ret_arr = select_points(xr_obj, lon, lat, method=method, tolerance=tolerance, tree=tree,
                                        return_distance=False)
            else:
                raise NotImplementedError("Only method='nearest' is currently supported.")
        else:
            raise ValueError("Both lat, lon are needed as indexers, else use path, region arguments or "
                             ".select_points(lon=..., lat=...) method.")
    elif region is not None:
        ret_arr = select_region(xr_obj, region)
    elif path is not None:
        if isinstance(path, Sequence) or isinstance(path, LineString):
            if isinstance(path, LineString):
                path = np.asarray(path).T
            else:
                path = np.asarray(path)

            if not np.ndim(path) == 2:
                raise ValueError('Path of more then 2 columns (lons, lats) is ambiguous, use dictionary instead')
            else:
                lon, lat = path
                ret_arr = select_points(xr_obj, lon, lat, method=method, tolerance=tolerance, tree=tree)
        elif isinstance(path, dict):
            ret_arr = select_points(xr_obj, method=method, tolerance=tolerance, tree=tree, **path)
        else:
            raise ValueError('Invalid path argument it can only be sequence of (lons, lats), shapely 2D LineString or'
                             'dictionary containing coords.')

    # xarray doesn't support slice indexer when method argument is passed.
    # allow mixing indexers with values and slices.
    slice_indexers = {dim: dim_val for dim, dim_val in indexers.items() if isinstance(dim_val, slice)}

    if slice_indexers:
        ret_arr = ret_arr.sel(**slice_indexers)
        # remove slice indexers from indexers
        for dim in slice_indexers.keys():
            indexers.pop(dim)

    return ret_arr.sel(**indexers, method=method)


# Accessors

# Dataset accessor

@xr.register_dataset_accessor("pyfesom2")
class FESOMDataset:
    """ A pyfesom2 Xarray accessor for FESOM datasets.
    """

    def __init__(self, xr_dataset: xr.Dataset):
        self._xrobj = xr_obj = xr_dataset
        # TODO: check valid fesom data? otherwise accessor is available on all xarray datasets
        self._tree_obj = None
        for datavar in xr_obj.data_vars.keys():
            setattr(self, str(datavar), FESOMDataArray(xr_obj[datavar], xr_obj))

    def select(self, method: str = 'nearest', tolerance: Optional[float] = None, region: Optional[Region] = None,
               path: Optional[Path] = None, **indexers):
        """A generalized interface to select data from an unstructured FESOM dataset.

        This method provides interface to similar to sel method of Xarray for an unstructured FESOM data. In addition
        there are additional arguments to select polygons and paths specified as Shapely's geometries. This method wraps
        select_region and select_points methods of this pyfesom2.accessor module.


        Parameters
        ----------
        method
            "nearest" (or geodesic-ally closest)  is currently supported.
        tolerance
            A tolerance radius to select non missing values, currently not supported.
        region
            As as length 4 sequence or Shapely's polygon geometries like Polygon or box.
        path
            A tuple of same-sized longitudes, latitudes or Shapely's LineString or a dictionary with keys as dimensions.
        indexers
            Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
            have to be dimensions of the dataset. These indexers are passed to xarray's sel method as-is.

        Returns
        -------
        xr.Dataset
        """

        sel_obj = select(self._xrobj, method=method, tolerance=tolerance, region=region, path=path, **indexers)
        return sel_obj

    def select_points(self, lon: ArrayLike, lat: ArrayLike, method: str = 'nearest',
                      tolerance: Optional[float] = None, **other_dims):
        """Returns a FESOM point dataset for specified longitudes and latitudes and other dimension representing
         a trajectory.

        This method selects points based on geodesic distance  to specified  lon, lat (and optionally to specified
        other dimensions). All arguments have to be of same shape and size. To select points geodesic-ally
        closest to input FESOM grid, both longitudes and latitudes of FESOM grid and and desired destination-transect points
        (arguments lon, lat) are projected onto geocentric coordinates.  This means tunnel distance as geedesic metric.
        A KDtree is used to efficiently select multiple points. For other orthogonal dimensions, default label based
        indexing of Xarray's sel method is used.

        Note
        ----
        This is unlike default Xarray's selection for rectilinear grids on longitudes, latitudes where cartesian distances
        are not used.
        Parameters
        ----------
        lon
            Array-like longitudes.
        lat
            Array-like latitudes.
        method
            "nearest" (or geodesic-ally closest)  is currently supported.
        tolerance
            A tolerance radius to select non missing values, currently not supported.
        other_dims
            Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
            have to be dimensions of dataarray or dataset.

        Returns
        -------
        xr.Dataset
            Returned dataset contains distance along trajectory in metric units (m or km) as a coordinate.
        """
        tree = self._tree
        return select_points(self._xrobj, lon, lat, method=method, tolerance=tolerance, tree=tree, return_distance=True,
                             **other_dims)

    def _build_tree(self):
        from cartopy.crs import Geocentric, Geodetic
        from scipy.spatial import cKDTree
        geocentric_crs, geodetic_crs = Geocentric(), Geodetic()
        src_pts = geocentric_crs.transform_points(geodetic_crs, np.asarray(self._xrobj.lon),
                                                  np.asarray(self._xrobj.lat))
        self._tree_obj = cKDTree(src_pts, leafsize=32, compact_nodes=False, balanced_tree=False)
        return self._tree_obj

    @property
    def _tree(self):
        """Property to regulate tree access, _tree to hide from jupyter notebook"""
        if self._tree_obj is not None:
            return self._tree_obj
        return self._build_tree()

    def __repr__(self):
        return self._xrobj.__repr__()

    def _repr_html_(self):
        return self._xrobj._repr_html_()


class FESOMDataArray:
    """ A wrapper around Dataarray, that passes dataset context around"""

    def __init__(self, xr_dataarray: xr.DataArray, context_dataset: Optional[xr.Dataset] = None):
        self._xrobj = xr_dataarray
        self._context_dataset = context_dataset
        self._native_projection = ccrs.PlateCarree()

    def select(self, method: str = 'nearest', tolerance: float = None, region: Optional[Region] = None,
               path: Optional[Path] = None, **indexers):
        """A generalized interface to select data from an unstructured FESOM dataset.

        This method provides interface to similar to sel method of Xarray for an unstructured FESOM data. In addition
        there are additional arguments to select polygons and paths specified as Shapely's geometries. This method wraps
        select_region and select_points methods of this pyfesom2.accessor module.


        Parameters
        ----------
        method
            "nearest" (or geodesic-ally closest)  is currently supported.
        tolerance
            A tolerance radius to select non missing values, currently not supported.
        region
            As as length 4 sequence or Shapely's polygon geometries like Polygon or box.
        path
            A tuple of same-sized longitudes, latitudes or Shapely's LineString or a dictionary with keys as dimensions.
        indexers
            Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
            have to be dimensions of the dataset. These indexers are passed to xarray's sel method as-is.

        Returns
        -------
        xr.Dataset
        """

        coords_dataset = self._context_dataset[['lon', 'lat', 'faces']]
        sel_obj = xr.merge([self._xrobj, coords_dataset])
        if "lat" in indexers or "lon" in indexers:
            tree = self._context_dataset.pyfesom2._tree
        else:
            tree = None
        sel_obj = select(sel_obj, method=method, tolerance=tolerance, region=region, path=path, tree=tree,
                         **indexers)
        return sel_obj

    def select_points(self, lon: Union[float, np.ndarray], lat: Union[float, np.ndarray], method: str = 'nearest',
                      tolerance: Optional[float] = None, **other_dims):
        """Returns a FESOM point dataarray for specified longitudes and latitudes and other dimension representing
         a trajectory.

        This method selects points geodesic-ally closest (default) specified to lon, lat and optionally to specified
        other dimensions as arguments. All arguments have to be of same shape and size. To select points geodesic-ally
        closest to input FESOM grid, both longitudes and latitudes of FESOM grid and and desired destination-transect
        points (arguments lon, lat) are projected onto geocentric coordinates.  A KDtree is used to efficiently select
        multiple points. Tree information is computed once and stored on context dataset of a variable.
        For other orthogonal dimensions label based indexing of Xarray's sel method is used.

        Note
        ----
        This is unlike default Xarray's selection for rectilinear grids on longitudes, latitudes where geodesic
        distances are not used.

        Parameters
        ----------
        lon
            Array-like longitudes.
        lat
            Array-like latitudes.
        method
            "nearest" (or geodesic-ally closest)  is currently supported.
        tolerance
            A tolerance radius to select non missing values, currently not supported.
        other_dims
            Additional arguments that define multi-dimensional transects. For example: time=..., nz1=... These arguments
            have to be dimensions of dataarray or dataset.

        Returns
        -------
        xr.DataArray

            Returned dataarray contains distance along trajectory in metric units (m or km) as a coordinate.
        """
        tree = self._context_dataset.pyfesom2._tree
        return select_points(self._xrobj, lon, lat, method=method, tolerance=tolerance, tree=tree, return_distance=True,
                             **other_dims)

    def __repr__(self):
        return f"Wrapped {self._xrobj.__repr__()}\n{super().__repr__()}"

    def _repr_html_(self):
        return self._xrobj._repr_html_()
