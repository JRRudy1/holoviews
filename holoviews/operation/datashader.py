from __future__ import absolute_import, division

from collections import Callable, Iterable
from distutils.version import LooseVersion
import warnings

import param
import numpy as np
import pandas as pd
import xarray as xr
import datashader as ds
import datashader.transfer_functions as tf
import dask.dataframe as dd

ds_version = LooseVersion(ds.__version__)

from datashader.core import bypixel
from datashader.pandas import pandas_pipeline
from datashader.dask import dask_pipeline
from datashape.dispatch import dispatch
from datashape import discover as dsdiscover

from ..core import (Operation, Element, Dimension, NdOverlay,
                    CompositeOverlay, Dataset)
from ..core.data import PandasInterface, DaskInterface, XArrayInterface
from ..core.sheetcoords import BoundingBox
from ..core.util import get_param_values, basestring
from ..element import Image, Path, Curve, Contours, RGB
from ..streams import RangeXY, PlotSize
from ..plotting.util import fire


class ResamplingOperation(Operation):
    """
    Abstract baseclass for resampling operations
    """

    dynamic = param.Boolean(default=True, doc="""
       Enables dynamic processing by default.""")

    expand = param.Boolean(default=True, doc="""
       Whether the x_range and y_range should be allowed to expand
       beyond the extent of the data.  Setting this value to True is
       useful for the case where you want to ensure a certain size of
       output grid, e.g. if you are doing masking or other arithmetic
       on the grids.  A value of False ensures that the grid is only
       just as large as it needs to be to contain the data, which will
       be faster and use less memory if the resulting aggregate is
       being overlaid on a much larger background.""")

    height = param.Integer(default=400, doc="""
       The height of the output image in pixels.""")

    width = param.Integer(default=400, doc="""
       The width of the output image in pixels.""")

    x_range  = param.NumericTuple(default=None, length=2, doc="""
       The x_range as a tuple of min and max x-value. Auto-ranges
       if set to None.""")

    y_range  = param.NumericTuple(default=None, length=2, doc="""
       The x_range as a tuple of min and max y-value. Auto-ranges
       if set to None.""")

    x_sampling = param.Number(default=None, doc="""
        Specifies the smallest allowed sampling interval along the y-axis.""")

    y_sampling = param.Number(default=None, doc="""
        Specifies the smallest allowed sampling interval along the y-axis.""")

    target = param.ClassSelector(class_=Image, doc="""
        A target Image which defines the desired x_range, y_range,
        width and height.
    """)

    streams = param.List(default=[PlotSize, RangeXY], doc="""
        List of streams that are applied if dynamic=True, allowing
        for dynamic interaction with the plot.""")

    element_type = param.ClassSelector(class_=(Dataset,), instantiate=False,
                                        is_instance=False, default=Image,
                                        doc="""
        The type of the returned Elements, must be a 2D Dataset type.""")

    link_inputs = param.Boolean(default=True, doc="""
        By default, the link_inputs parameter is set to True so that
        when applying shade, backends that support linked streams
        update RangeXY streams on the inputs of the shade operation.
        Disable when you do not want the resulting plot to be interactive,
        e.g. when trying to display an interactive plot a second time.""")

    def _get_sampling(self, element, x, y):
        target = self.p.target
        if target:
            x_range, y_range = target.range(x), target.range(y)
            height, width = target.dimension_values(2, flat=False).shape
        else:
            if x is None or y is None:
                x_range = self.p.x_range or (-0.5, 0.5)
                y_range = self.p.y_range or (-0.5, 0.5)
            else:
                if self.p.expand or not self.p.x_range:
                    x_range = self.p.x_range or element.range(x)
                else:
                    x0, x1 = self.p.x_range
                    ex0, ex1 = element.range(x)
                    x_range = max([x0, ex0]), min([x1, ex1])
                if self.p.expand or not self.p.y_range:
                    y_range = self.p.y_range or element.range(y)
                else:
                    y0, y1 = self.p.y_range
                    ey0, ey1 = element.range(y)
                    y_range = max([y0, ey0]), min([y1, ey1])
            width, height = self.p.width, self.p.height
        (xstart, xend), (ystart, yend) = x_range, y_range

        # Compute highest allowed sampling density
        xspan = xend - xstart
        yspan = yend - ystart
        if self.p.x_sampling:
            width = int(min([(xspan/self.p.x_sampling), width]))
        if self.p.y_sampling:
            height = int(min([(yspan/self.p.y_sampling), height]))
        xunit, yunit = float(xspan)/width, float(yspan)/height
        xs, ys = (np.linspace(xstart+xunit/2., xend-xunit/2., width),
                  np.linspace(ystart+yunit/2., yend-yunit/2., height))
        return (x_range, y_range), (xs, ys), (width, height)



class aggregate(ResamplingOperation):
    """
    aggregate implements 2D binning for any valid HoloViews Element
    type using datashader. I.e., this operation turns a HoloViews
    Element or overlay of Elements into an hv.Image or an overlay of
    hv.Images by rasterizing it, which provides a fixed-sized
    representation independent of the original dataset size.

    By default it will simply count the number of values in each bin
    but other aggregators can be supplied implementing mean, max, min
    and other reduction operations.

    The bins of the aggregate are defined by the width and height and
    the x_range and y_range. If x_sampling or y_sampling are supplied
    the operation will ensure that a bin is no smaller than the minimum
    sampling distance by reducing the width and height when zoomed in
    beyond the minimum sampling distance.

    By default, the PlotSize stream is applied when this operation
    is used dynamically, which means that the height and width
    will automatically be set to match the inner dimensions of
    the linked plot.
    """

    aggregator = param.ClassSelector(class_=ds.reductions.Reduction,
                                     default=ds.count())

    @classmethod
    def get_agg_data(cls, obj, category=None):
        """
        Reduces any Overlay or NdOverlay of Elements into a single
        xarray Dataset that can be aggregated.
        """
        paths = []
        kdims = list(obj.kdims)
        vdims = list(obj.vdims)
        dims = obj.dimensions(label=True)[:2]
        if isinstance(obj, Path):
            glyph = 'line'
            for p in obj.data:
                df = pd.DataFrame(p, columns=obj.dimensions('key', True))
                if isinstance(obj, Contours) and obj.vdims and obj.level:
                    df[obj.vdims[0].name] = p.level
                paths.append(df)
        elif isinstance(obj, CompositeOverlay):
            element = None
            for key, el in obj.data.items():
                x, y, element, glyph = cls.get_agg_data(el)
                dims = (x, y)
                df = PandasInterface.as_dframe(element)
                if isinstance(obj, NdOverlay):
                    df = df.assign(**dict(zip(obj.dimensions('key', True), key)))
                paths.append(df)
            if element is None:
                dims = None
            else:
                kdims += element.kdims
                vdims = element.vdims
        elif isinstance(obj, Element):
            glyph = 'line' if isinstance(obj, Curve) else 'points'
            paths.append(PandasInterface.as_dframe(obj))

        if dims is None or len(dims) != 2:
            return None, None, None, None
        else:
            x, y = dims

        if len(paths) > 1:
            if glyph == 'line':
                path = paths[0][:1]
                if isinstance(path, dd.DataFrame):
                    path = path.compute()
                empty = path.copy()
                empty.iloc[0, :] = (np.NaN,) * empty.shape[1]
                paths = [elem for p in paths for elem in (p, empty)][:-1]
            if all(isinstance(path, dd.DataFrame) for path in paths):
                df = dd.concat(paths)
            else:
                paths = [p.compute() if isinstance(p, dd.DataFrame) else p for p in paths]
                df = pd.concat(paths)
        else:
            df = paths[0]
        if category and df[category].dtype.name != 'category':
            df[category] = df[category].astype('category')

        for d in (x, y):
            if df[d].dtype.kind == 'M':
                param.main.warning('Casting %s dimension data to integer; '
                                   'datashader cannot process datetime data', d)
                df[d] = df[d].astype('int64') / 1000000.

        return x, y, Dataset(df, kdims=kdims, vdims=vdims), glyph


    def _aggregate_ndoverlay(self, element, agg_fn):
        """
        Optimized aggregation for NdOverlay objects by aggregating each
        Element in an NdOverlay individually avoiding having to concatenate
        items in the NdOverlay. Works by summing sum and count aggregates and
        applying appropriate masking for NaN values. Mean aggregation
        is also supported by dividing sum and count aggregates. count_cat
        aggregates are grouped by the categorical dimension and a separate
        aggregate for each category is generated.
        """
        # Compute overall bounds
        x, y = element.last.dimensions()[0:2]
        (x_range, y_range), (xs, ys), (width, height) = self._get_sampling(element, x, y)
        agg_params = dict({k: v for k, v in self.p.items() if k in aggregate.params()},
                          x_range=x_range, y_range=y_range)

        # Optimize categorical counts by aggregating them individually
        if isinstance(agg_fn, ds.count_cat):
            agg_params.update(dict(dynamic=False, aggregator=ds.count()))
            agg_fn1 = aggregate.instance(**agg_params)
            if element.ndims == 1:
                grouped = element
            else:
                grouped = element.groupby([agg_fn.column], container_type=NdOverlay,
                                          group_type=NdOverlay)
            return grouped.clone({k: agg_fn1(v) for k, v in grouped.items()})

        # Create aggregate instance for sum, count operations, breaking mean
        # into two aggregates
        column = agg_fn.column or 'Count'
        if isinstance(agg_fn, ds.mean):
            agg_fn1 = aggregate.instance(**dict(agg_params, aggregator=ds.sum(column)))
            agg_fn2 = aggregate.instance(**dict(agg_params, aggregator=ds.count()))
        else:
            agg_fn1 = aggregate.instance(**agg_params)
            agg_fn2 = None
        is_sum = isinstance(agg_fn1.aggregator, ds.sum)

        # Accumulate into two aggregates and mask
        agg, agg2, mask = None, None, None
        mask = None
        for v in element:
            # Compute aggregates and mask
            new_agg = agg_fn1.process_element(v, None)
            if is_sum:
                new_mask = np.isnan(new_agg.data[column].values)
                new_agg.data = new_agg.data.fillna(0)
            if agg_fn2:
                new_agg2 = agg_fn2.process_element(v, None)

            if agg is None:
                agg = new_agg
                if is_sum: mask = new_mask
                if agg_fn2: agg2 = new_agg2
            else:
                agg.data += new_agg.data
                if is_sum: mask &= new_mask
                if agg_fn2: agg2.data += new_agg2.data

        # Divide sum by count to compute mean
        if agg2 is not None:
            agg2.data.rename({'Count': agg_fn.column}, inplace=True)
            with np.errstate(divide='ignore', invalid='ignore'):
                agg.data /= agg2.data

        # Fill masked with with NaNs
        if is_sum:
            agg.data[column].values[mask] = np.NaN
        return agg


    def _process(self, element, key=None):
        agg_fn = self.p.aggregator
        category = agg_fn.column if isinstance(agg_fn, ds.count_cat) else None

        if (isinstance(element, NdOverlay) and
            ((isinstance(agg_fn, (ds.count, ds.sum, ds.mean)) and agg_fn.column not in element.kdims) or
             (isinstance(agg_fn, ds.count_cat) and agg_fn.column in element.kdims))):
            return self._aggregate_ndoverlay(element, agg_fn)

        x, y, data, glyph = self.get_agg_data(element, category)
        (x_range, y_range), (xs, ys), (width, height) = self._get_sampling(element, x, y)

        if x is None or y is None:
            xarray = xr.DataArray(np.full((height, width), np.NaN, dtype=np.float32),
                                  dims=['y', 'x'], coords={'x': xs, 'y': ys})
            return self.p.element_type(xarray)

        cvs = ds.Canvas(plot_width=width, plot_height=height,
                        x_range=x_range, y_range=y_range)

        column = agg_fn.column
        if column and isinstance(agg_fn, ds.count_cat):
            name = '%s Count' % agg_fn.column
        else:
            name = column
        vdims = [element.get_dimension(column)(name) if column
                 else Dimension('Count')]
        params = dict(get_param_values(element), kdims=element.dimensions()[:2],
                      datatype=['xarray'], vdims=vdims)

        dfdata = PandasInterface.as_dframe(data)
        agg = getattr(cvs, glyph)(dfdata, x, y, self.p.aggregator)
        if 'x_axis' in agg and 'y_axis' in agg:
            agg = agg.rename({'x_axis': x, 'y_axis': y})

        if agg.ndim == 2:
            # Replacing x and y coordinates to avoid numerical precision issues
            eldata = agg if ds_version > '0.5.0' else (xs, ys, agg.data)
            return self.p.element_type(eldata, **params)
        else:
            layers = {}
            for c in agg.coords[column].data:
                cagg = agg.sel(**{column: c})
                eldata = cagg if ds_version > '0.5.0' else (xs, ys, cagg.data)
                layers[c] = self.p.element_type(eldata, **params)
            return NdOverlay(layers, kdims=[data.get_dimension(column)])



class regrid(ResamplingOperation):
    """
    regrid allows resampling a HoloViews Image type using specified
    up- and downsampling functions defined using the aggregator and
    interpolation parameters respectively. By default upsampling is
    disabled to avoid unnecessarily upscaling an image that has to be
    sent to the browser. Also disables expanding the image beyond its
    original bounds avoiding unneccessarily padding the output array
    with nan values.
    """

    aggregator = param.ObjectSelector(default='mean',
        objects=['first', 'last', 'mean', 'mode', 'std', 'var', 'min', 'max'], doc="""
        Aggregation method.
        """)

    expand = param.Boolean(default=False, doc="""
       Whether the x_range and y_range should be allowed to expand
       beyond the extent of the data.  Setting this value to True is
       useful for the case where you want to ensure a certain size of
       output grid, e.g. if you are doing masking or other arithmetic
       on the grids.  A value of False ensures that the grid is only
       just as large as it needs to be to contain the data, which will
       be faster and use less memory if the resulting aggregate is
       being overlaid on a much larger background.""")

    interpolation = param.ObjectSelector(default='nearest',
        objects=['linear', 'nearest'], doc="""
        Interpolation method""")

    upsample = param.Boolean(default=False, doc="""
        Whether to allow upsampling if the source array is smaller
        than the requested array. Setting this value to True will
        enable upsampling using the interpolation method, when the
        requested width and height are larger than what is available
        on the source grid. If upsampling is disabled (the default)
        the width and height are clipped to what is available on the
        source array.""")

    def _process(self, element, key=None):
        if ds_version <= '0.5.0':
            raise RuntimeError('regrid operation requires datashader>=0.6.0')

        x, y = element.kdims
        (x_range, y_range), _, (width, height) = self._get_sampling(element, x, y)

        coords = tuple(element.dimension_values(d, expanded=False)
                       for d in [x, y])
        if element.interface is XArrayInterface and len(element.vdims) == 1:
            xarr = element.data[element.vdims[0].name]
        else:
            coord_dict = {x.name: coords[0], y.name: coords[1]}
            dims = [y.name, x.name]
            arrays = []
            for vd in element.vdims:
                arrays.append(element.dimension_values(vd, flat=False))
            if len(arrays) > 1:
                array = np.dstack(arrays).transpose([2, 0, 1])
                coord_dict['temp_vdim_layer'] = range(len(arrays))
                dims = ['temp_vdim_layer'] + dims
            else:
                array = arrays[0]
            xarr = xr.DataArray(array, coords=coord_dict, dims=dims)

        # Disable upsampling if requested
        (xstart, xend), (ystart, yend) = (x_range, y_range)
        xspan, yspan = (xend-xstart), (yend-ystart)
        if not self.p.upsample and self.p.target is None:
            (x0, x1), (y0, y1) = element.range(0), element.range(1)
            exspan, eyspan = (x1-x0), (y1-y0)
            width = min([int((xspan/exspan) * len(coords[0])), width])
            height = min([int((yspan/eyspan) * len(coords[1])), height])

        # Get expanded or bounded ranges
        cvs = ds.Canvas(plot_width=width, plot_height=height,
                        x_range=x_range, y_range=y_range)
        regridded = cvs.raster(xarr, upsample_method=self.p.interpolation,
                               downsample_method=self.p.aggregator)

        if regridded.ndim > 2:
            regridded = xr.Dataset({vd.name: regridded[i].drop('temp_vdim_layer')
                                    for i, vd in enumerate(element.vdims)})
        bbox = BoundingBox(points=[(xstart, ystart), (xend, yend)])
        xd = float(width) / xspan
        yd = float(height) / yspan
        return element.clone(regridded, datatype=['xarray'], bounds=bbox,
                             xdensity=xd, ydensity=yd)



class shade(Operation):
    """
    shade applies a normalization function followed by colormapping to
    an Image or NdOverlay of Images, returning an RGB Element.
    The data must be in the form of a 2D or 3D DataArray, but NdOverlays
    of 2D Images will be automatically converted to a 3D array.

    In the 2D case data is normalized and colormapped, while a 3D
    array representing categorical aggregates will be supplied a color
    key for each category. The colormap (cmap) may be supplied as an
    Iterable or a Callable.
    """

    cmap = param.ClassSelector(default=fire, class_=(Iterable, Callable, dict), doc="""
        Iterable or callable which returns colors as hex colors, to
        be used for the colormap of single-layer datashader output. 
        Callable type must allow mapping colors between 0 and 1.""")

    color_key = param.ClassSelector(class_=(Iterable, Callable, dict), doc="""
        Iterable or callable which returns colors as hex colors, to
        be used for the color key of categorical datashader output.
        Callable type must allow mapping colors between 0 and 1.""")

    normalization = param.ClassSelector(default='eq_hist',
                                        class_=(basestring, Callable),
                                        doc="""
        The normalization operation applied before colormapping.
        Valid options include 'linear', 'log', 'eq_hist', 'cbrt',
        and any valid transfer function that accepts data, mask, nbins
        arguments.""")

    clims = param.NumericTuple(default=None, length=2, doc="""
        Min and max data values to use for colormap interpolation, when
        wishing to override autoranging.
        """)

    link_inputs = param.Boolean(default=True, doc="""
        By default, the link_inputs parameter is set to True so that
        when applying shade, backends that support linked streams
        update RangeXY streams on the inputs of the shade operation.
        Disable when you do not want the resulting plot to be interactive,
        e.g. when trying to display an interactive plot a second time.""")

    @classmethod
    def concatenate(cls, overlay):
        """
        Concatenates an NdOverlay of Image types into a single 3D
        xarray Dataset.
        """
        if not isinstance(overlay, NdOverlay):
            raise ValueError('Only NdOverlays can be concatenated')
        xarr = xr.concat([v.data.T for v in overlay.values()],
                         pd.Index(overlay.keys(), name=overlay.kdims[0].name))
        params = dict(get_param_values(overlay.last),
                      vdims=overlay.last.vdims,
                      kdims=overlay.kdims+overlay.last.kdims)
        return Dataset(xarr.T, datatype=['xarray'], **params)


    @classmethod
    def uint32_to_uint8(cls, img):
        """
        Cast uint32 RGB image to 4 uint8 channels.
        """
        return np.flipud(img.view(dtype=np.uint8).reshape(img.shape + (4,)))


    @classmethod
    def rgb2hex(cls, rgb):
        """
        Convert RGB(A) tuple to hex.
        """
        if len(rgb) > 3:
            rgb = rgb[:-1]
        return "#{0:02x}{1:02x}{2:02x}".format(*(int(v*255) for v in rgb))


    def _process(self, element, key=None):
        if isinstance(element, NdOverlay):
            bounds = element.last.bounds
            element = self.concatenate(element)
        else:
            bounds = element.bounds

        vdim = element.vdims[0].name
        array = element.data[vdim]
        kdims = element.kdims

        # Compute shading options depending on whether
        # it is a categorical or regular aggregate
        shade_opts = dict(how=self.p.normalization)
        if element.ndims > 2:
            kdims = element.kdims[1:]
            categories = array.shape[-1]
            if not self.p.color_key:
                pass
            elif isinstance(self.p.color_key, dict):
                shade_opts['color_key'] = self.p.color_key
            elif isinstance(self.p.color_key, Iterable):
                shade_opts['color_key'] = [c for i, c in
                                           zip(range(categories), self.p.color_key)]
            else:
                colors = [self.p.color_key(s) for s in np.linspace(0, 1, categories)]
                shade_opts['color_key'] = map(self.rgb2hex, colors)
        elif not self.p.cmap:
            pass
        elif isinstance(self.p.cmap, Callable):
            colors = [self.p.cmap(s) for s in np.linspace(0, 1, 256)]
            shade_opts['cmap'] = map(self.rgb2hex, colors)
        else:
            shade_opts['cmap'] = self.p.cmap

        if self.p.clims:
            shade_opts['span'] = self.p.clims
        elif ds_version > '0.5.0' and self.p.normalization != 'eq_hist':
            shade_opts['span'] = element.range(vdim)

        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', r'invalid value encountered in true_divide')
            if np.isnan(array.data).all():
                arr = np.zeros(array.data.shape, dtype=np.uint32)
                img = array.copy()
                img.data = arr
            else:
                img = tf.shade(array, **shade_opts)
        params = dict(get_param_values(element), kdims=kdims,
                      bounds=bounds, vdims=RGB.vdims[:])
        return RGB(self.uint32_to_uint8(img.data), **params)



class datashade(aggregate, shade):
    """
    Applies the aggregate and shade operations, aggregating all
    elements in the supplied object and then applying normalization
    and colormapping the aggregated data returning RGB elements.

    See aggregate and shade operations for more details.
    """

    def _process(self, element, key=None):
        agg = aggregate._process(self, element, key)
        shaded = shade._process(self, agg, key)
        return shaded



class dynspread(Operation):
    """
    Spreading expands each pixel in an Image based Element a certain
    number of pixels on all sides according to a given shape, merging
    pixels using a specified compositing operator. This can be useful
    to make sparse plots more visible. Dynamic spreading determines
    how many pixels to spread based on a density heuristic.

    See the datashader documentation for more detail:

    http://datashader.readthedocs.io/en/latest/api.html#datashader.transfer_functions.dynspread
    """

    how = param.ObjectSelector(default='source',
                               objects=['source', 'over',
                                        'saturate', 'add'], doc="""
        The name of the compositing operator to use when combining
        pixels.""")

    max_px = param.Integer(default=3, doc="""
        Maximum number of pixels to spread on all sides.""")

    shape = param.ObjectSelector(default='circle', objects=['circle', 'square'],
                                 doc="""
        The shape to spread by. Options are 'circle' [default] or 'square'.""")

    threshold = param.Number(default=0.5, bounds=(0,1), doc="""
        When spreading, determines how far to spread.
        Spreading starts at 1 pixel, and stops when the fraction
        of adjacent non-empty pixels reaches this threshold.
        Higher values give more spreading, up to the max_px
        allowed.""")

    link_inputs = param.Boolean(default=True, doc="""
        By default, the link_inputs parameter is set to True so that
        when applying dynspread, backends that support linked streams
        update RangeXY streams on the inputs of the dynspread operation.
        Disable when you do not want the resulting plot to be interactive,
        e.g. when trying to display an interactive plot a second time.""")

    @classmethod
    def uint8_to_uint32(cls, img):
        shape = img.shape
        flat_shape = np.multiply.reduce(shape[:2])
        rgb = img.reshape((flat_shape, 4)).view('uint32').reshape(shape[:2])
        return rgb


    def _apply_dynspread(self, array):
        img = tf.Image(array)
        return tf.dynspread(img, max_px=self.p.max_px,
                            threshold=self.p.threshold,
                            how=self.p.how, shape=self.p.shape).data


    def _process(self, element, key=None):
        if not isinstance(element, RGB):
            raise ValueError('dynspread can only be applied to RGB Elements.')
        rgb = element.rgb
        new_data = {kd.name: rgb.dimension_values(kd, expanded=False)
                    for kd in rgb.kdims}
        rgbarray = np.dstack([element.dimension_values(vd, flat=False)
                              for vd in element.vdims])
        data = self.uint8_to_uint32(rgbarray)
        array = self._apply_dynspread(data)
        img = datashade.uint32_to_uint8(array)
        for i, vd in enumerate(element.vdims):
            if i < img.shape[-1]:
                new_data[vd.name] = np.flipud(img[..., i])
        return element.clone(new_data)


