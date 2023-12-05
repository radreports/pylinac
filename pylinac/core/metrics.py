from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import matplotlib.pyplot as plt
import numpy as np
from skimage import measure, segmentation
from skimage.measure._regionprops import RegionProperties
from skimage.segmentation import find_boundaries

from pylinac.core.array_utils import invert, stretch
from pylinac.core.geometry import Point

if TYPE_CHECKING:
    from pylinac.core.image import BaseImage


def is_symmetric(region: RegionProperties, *args, **kwargs) -> bool:
    """Whether the binary object's dimensions are symmetric, i.e. a perfect circle. Used to find the BB."""
    ymin, xmin, ymax, xmax = region.bbox
    y = abs(ymax - ymin)
    x = abs(xmax - xmin)
    if x > max(y * 1.05, y + 3) or x < min(y * 0.95, y - 3):
        return False
    return True


def is_near_center(region: RegionProperties, *args, **kwargs) -> bool:
    """Whether the bb is <2cm from the center of the field"""
    dpmm = kwargs["dpmm"]
    shape = kwargs["shape"]
    extent_limit_mm = 20
    bottom, left, top, right = region.bbox
    bb_center_x = left + (right - left) / 2
    bb_center_y = bottom + (top - bottom) / 2
    x_lo_limit = shape[1] / 2 - dpmm * extent_limit_mm
    x_hi_limit = shape[1] / 2 + dpmm * extent_limit_mm
    is_bb_x_centered = x_lo_limit < bb_center_x < x_hi_limit
    y_lo_limit = shape[0] / 2 - dpmm * extent_limit_mm
    y_hi_limit = shape[0] / 2 + dpmm * extent_limit_mm
    is_bb_y_centered = y_lo_limit < bb_center_y < y_hi_limit
    return is_bb_x_centered and is_bb_y_centered


def is_right_size_bb(region: RegionProperties, *args, **kwargs) -> bool:
    """Decide whether the ROI is roughly the size of a BB; not noise and not an artifact. Used to find the BB."""
    bb_area = region.area_filled / (kwargs["dpmm"] ** 2)
    bb_size = kwargs["bb_size"]  # radius in mm
    tolerance = kwargs["tolerance"]  # radius tolerance in mm
    # A = pi * r^2
    larger_bb_area = np.pi * (bb_size + tolerance) ** 2
    smaller_bb_area = max(
        (np.pi * (bb_size - tolerance) ** 2, 2)
    )  # set a min of 2 to avoid a lower bound of 0 when radius<=2. Having a small lower bound is much more likely to find noise in a block.
    # this is actually really important. A lower bound of 1 will catch SIGNIFICANT noise and produce erroneous results.
    return smaller_bb_area < bb_area < larger_bb_area


def is_solid(region: RegionProperties, *args, **kwargs) -> bool:
    """Whether the ROI is spiculated. We want nice, round ROIs,
    and this will drop such ROIs. Generally, these spiculations are noise or a BB rod.
    """
    return region.solidity > 0.9


def is_round(region: RegionProperties, *args, **kwargs) -> bool:
    """Decide if the ROI is circular in nature by testing the filled area vs bounding box. Used to find the BB."""
    expected_fill_ratio = np.pi / 4  # area of a circle inside a square
    actual_fill_ratio = region.filled_area / region.bbox_area
    return expected_fill_ratio * 1.2 > actual_fill_ratio > expected_fill_ratio * 0.8


def is_right_circumference(region: RegionProperties, *args, **kwargs) -> bool:
    """Test the regionprop's perimeter attr to see if it matches
    that of an equivalent circle"""
    upper_circumference = 2 * np.pi * (kwargs["bb_size"] + kwargs["tolerance"])
    lower_circumference = 2 * np.pi * (kwargs["bb_size"] - kwargs["tolerance"])
    actual_perimeter = region.perimeter / kwargs["dpmm"]
    return upper_circumference > actual_perimeter > lower_circumference


def is_right_square_perimeter(region: RegionProperties, *args, **kwargs) -> bool:
    """Test the regionprop's perimeter attr to see if it matches
    that of an equivalent square. In reality, edges aren't perfectly straight, so
    the real perimeter is always going to be higher than the theoretical perimeter.
    We thus add a larger tolerance (20%) to the upper perimeter"""
    actual_perimeter = region.perimeter / kwargs["dpmm"]
    upper_perimeter = 1.20 * 2 * (
        kwargs["field_width_mm"] + kwargs["field_tolerance_mm"]
    ) + 2 * (kwargs["field_height_mm"] + kwargs["field_tolerance_mm"])
    lower_perimeter = 2 * (
        kwargs["field_width_mm"] - kwargs["field_tolerance_mm"]
    ) + 2 * (kwargs["field_height_mm"] - kwargs["field_tolerance_mm"])
    return upper_perimeter > actual_perimeter > lower_perimeter


def is_square(region: RegionProperties, *args, **kwargs) -> bool:
    """Decide if the ROI is square in nature by testing the filled area vs bounding box. Used to find the BB."""
    actual_fill_ratio = region.filled_area / region.bbox_area
    return actual_fill_ratio > 0.8


def is_right_area_square(region: RegionProperties, *args, **kwargs) -> bool:
    """Decide if the ROI is square in nature by testing the filled area vs bounding box. Used to find the BB."""
    field_area = region.area_filled / (kwargs["dpmm"] ** 2)
    low_bound_expected_area = (
        kwargs["field_width_mm"] - kwargs["field_tolerance_mm"]
    ) * (kwargs["field_height_mm"] - kwargs["field_tolerance_mm"])
    high_bound_expected_area = (
        kwargs["field_width_mm"] + kwargs["field_tolerance_mm"]
    ) * (kwargs["field_height_mm"] + kwargs["field_tolerance_mm"])
    return low_bound_expected_area < field_area < high_bound_expected_area


def deduplicate_points_and_boundaries(
    original_points: list[Point],
    new_points: list[Point],
    min_separation_px: float,
    original_boundaries: list[np.ndarray],
    new_boundaries: list[np.ndarray],
) -> (list[Point], list[np.ndarray]):
    """Deduplicate points that are too close together. The original points should be the
    starting point. The new point's x, y, and z values are compared to the existing points.
    If the new point is too close to the original point, it's dropped. If it's sufficiently
    far away, it is added. Will return a new combined list of points.

    We assume the original points are already deduplicated. When used in a loop starting from an empty list
    this is true."""
    combined_points = original_points
    combined_boundaries = original_boundaries
    for new_point, new_boundary in zip(new_points, new_boundaries):
        for original_point in original_points:
            if new_point.distance_to(original_point) < min_separation_px:
                break
        else:
            combined_points.append(new_point)
            combined_boundaries.append(new_boundary)
    return combined_points, combined_boundaries


class MetricBase(ABC):
    """Base class for any 2D metric. This class is abstract and should not be instantiated.

    The subclass should implement the ``calculate`` method and the ``name`` attribute.

    As a best practice, the ``image_compatibility`` attribute should be set to a list of image classes that the metric
    is compatible with. Image types that are not in the list will raise an error. This allows
    compatibility to be explicit. However, by default this is None and no compatibility checking is done.
    """

    unit: str = ""
    image: BaseImage
    image_compatibility: list[BaseImage] | None = None
    name: str

    def inject_image(self, image: BaseImage):
        """Inject the image into the metric."""
        if self.image_compatibility is not None and not isinstance(
            image, self.image_compatibility
        ):
            raise TypeError(f"Image must be one of {self.image_compatibility}")
        self.image = image

    def context_calculate(self) -> Any:
        """Calculate the metric. This also checks the image hash to attempt to ensure no changes were made."""
        img_hash = hash(self.image.array.tobytes())
        calculation = self.calculate()
        # check no modifications
        if hash(self.image.array.tobytes()) != img_hash:
            raise RuntimeError(
                "A metric modified an image. This is not allowed as this could affect other, downstream metrics. Change"
                "the calculate method to not modify the underlying image."
            )
        return calculation

    @abstractmethod
    def calculate(self) -> Any:
        """Calculate the metric. Can return anything"""
        pass

    def plot(self, axis: plt.Axes, **kwargs) -> None:
        """Plot the metric"""
        pass

    def additional_plots(self) -> list[plt.figure]:
        """Plot additional information on a separate figure as needed.

        This should NOT show the figure. The figure will be shown
        via the ``metric_plots`` method. Calling show here would
        block other metrics from plotting their own separate metrics.
        """
        pass


class GlobalSizedDiskLocator(MetricBase):
    name: str
    points: list[Point]
    y_boundaries: list[np.ndarray]
    x_boundaries: list[np.ndarray]

    def __init__(
        self,
        radius_mm: float,
        radius_tolerance_mm: float,
        detection_conditions: list[Callable[[RegionProperties, ...], bool]] = (
            is_round,
            is_right_size_bb,
            is_right_circumference,
        ),
        min_number: int = 1,
        max_number: int | None = None,
        min_separation_mm: float = 5,
        name="Global Disk Locator",
    ):
        """Finds BBs globally within an image.

        Parameters
        ----------
        radius_mm : float
            The radius of the BB in mm.
        radius_tolerance_mm : float
            The tolerance of the BB radius in mm.
        detection_conditions : list[callable]
            A list of functions that take a regionprops object and return a boolean.
            The functions should be used to determine whether the regionprops object
            is a BB.
        min_number : int
            The minimum number of BBs to find. If not found, an error is raised.
        max_number : int, None
            The maximum number of BBs to find. If None, no maximum is set.
        min_separation_mm : float
            The minimum distance between BBs in mm. If BBs are found that are closer than this,
            they are deduplicated.
        name : str
            The name of the metric.
        """
        self.radius = radius_mm
        self.radius_tolerance = radius_tolerance_mm
        self.detection_conditions = detection_conditions
        self.name = name
        self.min_number = min_number
        self.max_number = max_number or 1e3
        self.min_separation_mm = min_separation_mm

    def _calculate_sample(
        self, sample: np.ndarray, top_offset: int, left_offset: int
    ) -> (list[Point], list[np.ndarray], list[RegionProperties]):
        """Find up to N BBs/disks in the image. This will look for BBs at every percentile range.
        Multiple BBs may be found at different threshold levels."""

        # The implementation difference here from the original isn't large,
        # But we need to detect MULTIPLE bbs instead of just one.
        bbs = []
        boundaries = []
        detected_regions = {}
        # uses the same algo as original WL; this is better than a percentile method as the percentile method
        # can often be thrown off at the very ends of the distribution. It's more linear and faster to use the simple
        # spread of min/max.
        sample = stretch(sample, min=0, max=1)
        imin, imax = sample.min(), sample.max()
        spread = imax - imin
        step_size = (
            spread / 50
        )  # move in 1/50 increments; maximum of 50 passes per image
        cutoff = (
            imin + step_size
        )  # start at the min + 1 step; we know the min cutoff will be a blank, full image
        while cutoff <= imax and len(bbs) < self.max_number:
            try:
                binary_array = sample > cutoff
                labeled_arr = measure.label(binary_array, connectivity=1)
                regions = measure.regionprops(labeled_arr, intensity_image=sample)
                detected_regions = {i: r for i, r in enumerate(regions)}
                for condition in self.detection_conditions:
                    to_pop = []
                    for key, region in sorted(
                        detected_regions.items(),
                        key=lambda item: item[1].filled_area,
                        reverse=True,
                    ):
                        if not condition(
                            region,
                            dpmm=self.image.dpmm,
                            bb_size=self.radius,
                            tolerance=self.radius_tolerance,
                            shape=binary_array.shape,
                        ):
                            to_pop.append(key)
                    detected_regions = {
                        key: region
                        for key, region in detected_regions.items()
                        if key not in to_pop
                    }
                if len(detected_regions) == 0:
                    raise ValueError
                else:
                    points = [
                        Point(region.weighted_centroid[1], region.weighted_centroid[0])
                        for region in detected_regions.values()
                    ]
                    new_boundaries = [
                        get_boundary(
                            detected_region,
                            top_offset=top_offset,
                            left_offset=left_offset,
                        )
                        for detected_region in detected_regions.values()
                    ]
                    bbs, boundaries = deduplicate_points_and_boundaries(
                        original_points=bbs,
                        new_points=points,
                        min_separation_px=self.min_separation_mm * self.image.dpmm,
                        original_boundaries=boundaries,
                        new_boundaries=new_boundaries,
                    )
            except (IndexError, ValueError):
                pass
            finally:
                cutoff += step_size
        if len(bbs) < self.min_number:
            # didn't find the number we needed
            raise ValueError(
                f"Couldn't find the minimum number of disks in the image. Found {len(bbs)}; required: {self.min_number}"
            )
        return bbs, boundaries, list(detected_regions.values())

    def calculate(self) -> list[Point]:
        """Find up to N BBs/disks in the image. This will look for BBs at every percentile range.
        Multiple BBs may be found at different threshold levels."""
        sample = invert(self.image.array)
        self.points, boundaries, _ = self._calculate_sample(
            sample, top_offset=0, left_offset=0
        )
        self.y_boundaries = []
        self.x_boundaries = []
        for boundary in boundaries:
            boundary_y, boundary_x = np.nonzero(boundary)
            self.y_boundaries.append(boundary_y)
            self.x_boundaries.append(boundary_x)
        return self.points

    def plot(
        self,
        axis: plt.Axes,
        show_boundaries: bool = True,
        color: str = "red",
        markersize: float = 3,
        alpha: float = 0.25,
    ) -> None:
        """Plot the BB centers"""
        for point in self.points:
            axis.plot(point.x, point.y, "o", color=color)
        if show_boundaries:
            for boundary_y, boundary_x in zip(self.y_boundaries, self.x_boundaries):
                axis.scatter(
                    boundary_x,
                    boundary_y,
                    c=color,
                    marker="s",
                    alpha=alpha,
                    s=markersize,
                )


class SizedDiskRegion(GlobalSizedDiskLocator):
    """A metric to find a disk/BB in an image where the BB is near an expected position and size.
    This will calculate the scikit-image regionprops of the BB."""

    x_offset: float
    y_offset: float
    is_from_physical: bool
    is_from_center: bool
    max_number = 1
    min_number = 1
    min_separation_mm = 1e4

    def __init__(
        self,
        expected_position: Point | tuple[float, float],
        search_window: tuple[float, float],
        radius: float,
        radius_tolerance: float,
        detection_conditions: list[Callable[[RegionProperties, ...], bool]] = (
            is_right_size_bb,
            is_round,
            is_right_circumference,
            is_symmetric,
            is_solid,
        ),
        invert: bool = True,
        name: str = "Disk Region",
    ):
        # purposely avoid super call as parent defaults to mm. We set the values ourselves.
        self.expected_position = Point(expected_position)
        self.radius = radius
        self.radius_tolerance = radius_tolerance
        self.search_window = search_window
        self.detection_conditions = detection_conditions
        self.name = name
        self.invert = invert
        self.is_from_center = False
        self.is_from_physical = False

    @classmethod
    def from_physical(
        cls,
        expected_position_mm: Point | tuple[float, float],
        search_window_mm: tuple[float, float],
        radius_mm: float,
        radius_tolerance_mm: float,
        detection_conditions: list[Callable[[RegionProperties, ...], bool]] = (
            is_right_size_bb,
            is_round,
            is_right_circumference,
            is_symmetric,
            is_solid,
        ),
        invert: bool = True,
        name="Disk Region",
    ):
        """Create a DiskRegion using physical dimensions."""
        # We set a flag so we know to convert from physical sizes to pixels later.
        # We don't have the image/dpmm yet so we can't do it now.
        instance = cls(
            expected_position=expected_position_mm,
            search_window=search_window_mm,
            radius=radius_mm,
            radius_tolerance=radius_tolerance_mm,
            detection_conditions=detection_conditions,
            name=name,
            invert=invert,
        )
        instance.is_from_physical = True
        return instance

    @classmethod
    def from_center(
        cls,
        expected_position: Point | tuple[float, float],
        search_window: tuple[float, float],
        radius: float,
        radius_tolerance: float,
        detection_conditions: list[Callable[[RegionProperties, ...], bool]] = (
            is_right_size_bb,
            is_round,
            is_right_circumference,
            is_symmetric,
            is_solid,
        ),
        invert: bool = True,
        name="Disk Region",
    ):
        """Create a DiskRegion from a center point."""
        # We set a flag so we know to convert from image edge to center later.
        # We don't have the image/dpmm yet so we can't do it now
        instance = cls(
            expected_position=expected_position,
            search_window=search_window,
            radius=radius,
            radius_tolerance=radius_tolerance,
            detection_conditions=detection_conditions,
            name=name,
            invert=invert,
        )
        instance.is_from_center = True
        return instance

    @classmethod
    def from_center_physical(
        cls,
        expected_position_mm: Point | tuple[float, float],
        search_window_mm: tuple[float, float],
        radius_mm: float,
        radius_tolerance_mm: float = 0.25,
        detection_conditions: list[Callable[[RegionProperties, ...], bool]] = (
            is_right_size_bb,
            is_round,
            is_right_circumference,
            is_symmetric,
            is_solid,
        ),
        invert: bool = True,
        name="Disk Region",
    ):
        """Create a DiskRegion using physical dimensions from the center point."""
        # We set a flag so we know to convert from physical sizes to pixels later.
        # We don't have the image/dpmm yet so we can't do it now
        instance = cls(
            expected_position=expected_position_mm,
            search_window=search_window_mm,
            radius=radius_mm,
            radius_tolerance=radius_tolerance_mm,
            detection_conditions=detection_conditions,
            name=name,
            invert=invert,
        )
        instance.is_from_physical = True
        instance.is_from_center = True
        return instance

    def calculate(self) -> RegionProperties:
        """Find the scikit-image regiongprops of the BB.

        This will apply a high-pass filter to the image iteratively.
        The filter starts at a very low percentile and increases until
        a region is found that meets the detection conditions.
        """
        if self.is_from_physical:
            # convert from physical sizes to pixels
            self.expected_position * self.image.dpmm
            self.search_window = np.asarray(self.search_window) * self.image.dpmm
        else:
            # convert from pixels to physical sizes
            # I know, it's weird. The functions
            # for detection historically have expected
            # sizes in physical dimensions
            self.radius /= self.image.dpmm
            self.radius_tolerance /= self.image.dpmm
        if self.is_from_center:
            # convert from image edge to center
            self.expected_position.x += self.image.shape[1] / 2
            self.expected_position.y += self.image.shape[0] / 2
        # sample the image in the search window; need to convert to mm
        left = math.floor(self.expected_position.x - self.search_window[0] / 2)
        right = math.ceil(self.expected_position.x + self.search_window[0] / 2)
        top = math.floor(self.expected_position.y - self.search_window[1] / 2)
        bottom = math.ceil(self.expected_position.y + self.search_window[1] / 2)
        sample = self.image[top:bottom, left:right]
        # we might need to invert the image so that the BB pixel intensity is higher than the background
        if self.invert:
            sample = invert(sample)
        points, boundaries, regions = self._calculate_sample(
            sample, top_offset=top, left_offset=left
        )
        self.x_offset = left
        self.y_offset = top
        y_boundary, x_boundary = np.nonzero(boundaries[0])
        self.y_boundaries = [y_boundary]
        self.x_boundaries = [x_boundary]
        return regions[0]

    def plot(
        self,
        axis: plt.Axes,
        show_boundaries: bool = True,
        color: str = "red",
        markersize: float = 3,
        alpha: float = 0.25,
    ) -> None:
        """Plot the BB center"""
        if show_boundaries:
            axis.scatter(
                self.x_boundaries[0],
                self.y_boundaries[0],
                c=color,
                marker="s",
                alpha=alpha,
                s=markersize,
            )


class SizedDiskLocator(SizedDiskRegion):
    """Calculates the weighted centroid of a disk/BB as a Point in an image where the disk is near an expected position and size."""

    point: Point

    def calculate(self) -> Point:
        """Get the weighted centroid of the region prop of the BB."""
        region = super().calculate()
        self.point = Point(
            region.weighted_centroid[1] + self.x_offset,
            region.weighted_centroid[0] + self.y_offset,
        )
        return self.point

    def plot(
        self,
        axis: plt.Axes,
        show_boundaries: bool = True,
        color: str = "red",
        markersize: float = 3,
        alpha: float = 0.25,
    ) -> None:
        """Plot the BB center"""
        super().plot(
            axis,
            show_boundaries=show_boundaries,
            color=color,
            markersize=markersize,
            alpha=alpha,
        )
        axis.plot(self.point.x, self.point.y, "o", color=color, markersize=10)


class GlobalSizedFieldLocator(MetricBase):
    fields: list[Point]
    boundaries: list[np.ndarray]
    is_from_physical: bool = False

    def __init__(
        self,
        field_width_px: float,
        field_height_px: float,
        field_tolerance_px: float,
        min_number: int = 1,
        max_number: int | None = None,
        name: str = "Field Finder",
        detection_conditions: list[callable] = (
            is_right_square_perimeter,
            is_right_area_square,
        ),
    ):
        """Finds fields globally within an image.

        Parameters
        ----------
        field_width_px : float
            The width of the field in px.
        field_height_px : float
            The height of the field in px.
        field_tolerance_px : float
            The tolerance of the field size in px.
        min_number : int
            The minimum number of fields to find. If not found, an error is raised.
        max_number : int, None
            The maximum number of fields to find. If None, no maximum is set.
        name : str
            The name of the metric.
        detection_conditions : list[callable]
            A list of functions that take a regionprops object and return a boolean.
        """
        self.field_width_mm = field_width_px
        self.field_height_mm = field_height_px
        self.field_tolerance_mm = field_tolerance_px
        self.min_number = min_number
        self.max_number = max_number or 1e6
        self.name = name
        self.detection_conditions = detection_conditions

    @classmethod
    def from_physical(
        cls,
        field_width_mm: float,
        field_height_mm: float,
        field_tolerance_mm: float,
        min_number: int = 1,
        max_number: int | None = None,
        name: str = "Field Finder",
        detection_conditions: list[callable] = (
            is_right_square_perimeter,
            is_right_area_square,
        ),
    ):
        """Construct an instance using physical dimensions.

        Parameters
        ----------
        field_width_mm : float
            The width of the field in mm.
        field_height_mm : float
            The height of the field in mm.
        field_tolerance_mm : float
            The tolerance of the field size in mm.
        min_number : int
            The minimum number of fields to find. If not found, an error is raised.
        max_number : int, None
            The maximum number of fields to find. If None, no maximum is set.
        name : str
            The name of the metric.
        detection_conditions : list[callable]
            A list of functions that take a regionprops object and return a boolean.
        """
        instance = cls(
            field_width_px=field_width_mm,
            field_height_px=field_height_mm,
            field_tolerance_px=field_tolerance_mm,
            min_number=min_number,
            max_number=max_number,
            name=name,
            detection_conditions=detection_conditions,
        )
        instance.is_from_physical = True
        return instance

    def calculate(self) -> list[Point]:
        """Find up to N fields in the image. This will look for fields at every percentile range.
        Multiple fields may be found at different threshold levels."""
        if not self.is_from_physical:
            self.field_width_mm /= self.image.dpmm
            self.field_height_mm /= self.image.dpmm
            self.field_tolerance_mm /= self.image.dpmm
        fields = []
        boundaries = []
        sample = self.image.array
        # search for multiple BBs by iteratively raising the high-pass threshold value.
        imin, imax = sample.min(), sample.max()
        spread = imax - imin
        step_size = (
            spread / 50
        )  # move in 1/50 increments; maximum of 50 passes per image
        cutoff = imin + step_size * 5  # start at 10% height
        while cutoff <= imax and len(fields) < self.max_number:
            try:
                binary_array = sample > cutoff
                binary_array = segmentation.clear_border(binary_array, buffer_size=3)
                labeled_arr = measure.label(binary_array)
                regions = measure.regionprops(labeled_arr, intensity_image=sample)
                conditions_met = [
                    all(
                        condition(
                            region,
                            dpmm=self.image.dpmm,
                            field_width_mm=self.field_width_mm,
                            field_height_mm=self.field_height_mm,
                            field_tolerance_mm=self.field_tolerance_mm,
                            shape=binary_array.shape,
                        )
                        for condition in self.detection_conditions
                    )
                    for region in regions
                ]
                if not any(conditions_met):
                    raise ValueError
                else:
                    fields_regions = [
                        regions[idx]
                        for idx, value in enumerate(conditions_met)
                        if value
                    ]
                    points = [
                        Point(region.centroid[1], region.centroid[0])
                        for region in fields_regions
                    ]
                    # find the boundaries of the fields
                    # this is solely for plotting purposes
                    # these will be bool arrays
                    # we pad the boundaries to offset the ROI to the right
                    # position on the image.
                    new_boundaries = [
                        get_boundary(region, top_offset=0, left_offset=0)
                        for region in fields_regions
                    ]
                    # the separation is the minimum value + field size
                    fields, boundaries = deduplicate_points_and_boundaries(
                        original_points=fields,
                        new_points=points,
                        min_separation_px=max(
                            r.equivalent_diameter_area for r in fields_regions
                        )
                        / self.image.dpmm,
                        original_boundaries=boundaries,
                        new_boundaries=new_boundaries,
                    )
            except (IndexError, ValueError):
                pass
            finally:
                cutoff += step_size
        if len(fields) < self.min_number:
            # didn't find the number we needed
            raise ValueError(
                f"Couldn't find the minimum number of fields in the image. Found {len(fields)}; required: {self.min_number}"
            )
        self.fields = fields
        self.boundaries = boundaries
        return fields

    def plot(
        self,
        axis: plt.Axes,
        show_boundaries: bool = True,
        color: str = "red",
        markersize: float = 3,
        alpha: float = 0.25,
    ) -> None:
        """Plot the BB centers and boundary of detection."""
        for point in self.fields:
            axis.plot(point.x, point.y, color=color, marker="+", alpha=alpha)
        if show_boundaries:
            for boundary in self.boundaries:
                boundary_y, boundary_x = np.nonzero(boundary)
                axis.scatter(
                    boundary_x,
                    boundary_y,
                    c=color,
                    marker="s",
                    alpha=alpha,
                    s=markersize,
                )


class GlobalFieldLocator(GlobalSizedFieldLocator):
    def __init__(
        self,
        min_number: int = 1,
        max_number: int | None = None,
        name: str = "Field Finder",
        detection_conditions: list[callable] = (
            is_right_square_perimeter,
            is_right_area_square,
        ),
    ):
        """Finds fields globally within an image, irrespective of size."""
        # we override to set the width/height/tolerance to be very large
        # in this case we are more likely to get noise since the size is unconstrained.
        super().__init__(
            field_width_px=1e4,
            field_height_px=1e4,
            field_tolerance_px=1e4,
            min_number=min_number,
            max_number=max_number,
            name=name,
            detection_conditions=detection_conditions,
        )

    @classmethod
    def from_physical(
        cls,
        *args,
        **kwargs,
    ):
        raise NotImplementedError(
            "This method is not implemented for global field-finding. Use the "
            "standard initializer instead."
        )


def get_boundary(
    region: RegionProperties, top_offset: int, left_offset: int
) -> np.ndarray:
    """Find the boundary of the region as the absolute position in the image.
    This will calculate the outline of the region. Mostly used for plotting."""
    # we pad the boundaries to offset the ROI to the right and down to the absolute position
    # on the image.
    return np.pad(
        find_boundaries(
            # padding is needed as boundary edges aren't detected otherwise
            np.pad(
                region.image,
                pad_width=1,
                mode="constant",
                constant_values=0,
            ),
            connectivity=region.image.ndim,
            mode="inner",
            background=0,
        ),
        ((region.bbox[0] + top_offset - 1, 0), (region.bbox[1] + left_offset - 1, 0)),
        mode="constant",
        constant_values=0,
    )