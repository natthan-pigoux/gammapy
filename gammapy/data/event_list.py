# Licensed under a 3-clause BSD style license - see LICENSE.rst
import collections
import copy
import html
import logging
import warnings
import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, Angle, SkyCoord, angular_separation
from astropy.table import vstack as vstack_tables
from astropy.visualization import quantity_support
import matplotlib.pyplot as plt
from gammapy.maps import MapAxis, MapCoord, RegionGeom, WcsNDMap
from gammapy.maps.axes import UNIT_STRING_FORMAT
from gammapy.utils.fits import earth_location_from_dict
from gammapy.utils.testing import Checker
from gammapy.utils.time import time_ref_from_dict
from .metadata import EventListMetaData
from gammapy.utils.deprecation import deprecated_renamed_argument

__all__ = ["EventList"]

log = logging.getLogger(__name__)


class EventList:
    """Event list.

    Event list data is stored as ``table`` (`~astropy.table.Table`) data member.

    The most important reconstructed event parameters
    are available as the following columns:

    - ``TIME`` - Mission elapsed time (sec)
    - ``RA``, ``DEC`` - ICRS system position (deg)
    - ``ENERGY`` - Energy (usually MeV for Fermi and TeV for IACTs)

    Note that ``TIME`` is usually sorted, but sometimes it is not.
    E.g. when simulating data, or processing it in certain ways.
    So generally any analysis code should assume ``TIME`` is not sorted.

    Other optional (columns) that are sometimes useful for high level analysis:

    - ``GLON``, ``GLAT`` - Galactic coordinates (deg)
    - ``DETX``, ``DETY`` - Field of view coordinates (deg)

    Note that when reading data for analysis you shouldn't use those
    values directly, but access them via properties which create objects
    of the appropriate class:

    - `time` for ``TIME``
    - `radec` for ``RA``, ``DEC``
    - `energy` for ``ENERGY``
    - `galactic` for ``GLON``, ``GLAT``

    Parameters
    ----------
    table : `~astropy.table.Table`
        Event list table.
    meta : `~gammapy.data.EventListMetaData`
        The metadata. Default is None.

    Examples
    --------
    >>> from gammapy.data import EventList
    >>> events = EventList.read("$GAMMAPY_DATA/cta-1dc/data/baseline/gps/gps_baseline_110380.fits")
    >>> print(events)
    EventList
    ---------
    <BLANKLINE>
      Instrument       : None
      Telescope        : CTA
      Obs. ID          : 110380
    <BLANKLINE>
      Number of events : 106217
      Event rate       : 59.273 1 / s
    <BLANKLINE>
      Time start       : 59235.5
      Time stop        : 59235.52074074074
    <BLANKLINE>
      Min. energy      : 3.00e-02 TeV
      Max. energy      : 1.46e+02 TeV
      Median energy    : 1.02e-01 TeV
    <BLANKLINE>
      Max. offset      : 5.0 deg
    <BLANKLINE>

    """

    def __init__(self, table, meta=None):
        self.table = table
        self.meta = meta or EventListMetaData()

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"

    @classmethod
    def read(cls, filename, hdu="EVENTS", checksum=False, **kwargs):
        """Read from FITS file.

        Format specification: :ref:`gadf:iact-events`

        Parameters
        ----------
        filename : `pathlib.Path`, str
            Filename
        hdu : str
            Name of events HDU. Default is "EVENTS".
        checksum : bool
            If True checks both DATASUM and CHECKSUM cards in the file headers. Default is False.
        """
        from gammapy.data.io import EventListReader

        return EventListReader(hdu, checksum).read(filename)

    def to_table_hdu(self, format="gadf"):
        """
        Convert event list to a `~astropy.io.fits.BinTableHDU`.

        Parameters
        ----------
        format : str, optional
            Output format, currently only "gadf" is supported. Default is "gadf".

        Returns
        -------
        hdu : `astropy.io.fits.BinTableHDU`
            EventList converted to FITS representation.
        """
        from gammapy.data.io import EventListWriter

        return EventListWriter().to_hdu(self, format)

    # TODO: Pass metadata here. Also check that specific meta contents are consistent
    @classmethod
    def from_stack(cls, event_lists, **kwargs):
        """Stack (concatenate) list of event lists.

        Calls `~astropy.table.vstack`.

        Parameters
        ----------
        event_lists : list
            List of `~gammapy.data.EventList` to stack.
        **kwargs : dict, optional
            Keyword arguments passed to `~astropy.table.vstack`.
        """
        tables = [_.table for _ in event_lists]
        stacked_table = vstack_tables(tables, **kwargs)
        log.warning("The meta information will be empty here.")
        return cls(stacked_table)

    def stack(self, other):
        """Stack with another EventList in place.

        Calls `~astropy.table.vstack`.

        Parameters
        ----------
        other : `~gammapy.data.EventList`
            Event list to stack to self.
        """
        self.table = vstack_tables([self.table, other.table])

    def __str__(self):
        info = self.__class__.__name__ + "\n"
        info += "-" * len(self.__class__.__name__) + "\n\n"

        instrument = self.table.meta.get("INSTRUME")
        info += f"\tInstrument       : {instrument}\n"

        telescope = self.table.meta.get("TELESCOP")
        info += f"\tTelescope        : {telescope}\n"

        obs_id = self.table.meta.get("OBS_ID", "")
        info += f"\tObs. ID          : {obs_id}\n\n"

        info += f"\tNumber of events : {len(self.table)}\n"
        if self.table.meta.get("TSTART", False):
            rate = len(self.table) / self.observation_time_duration
            info += f"\tEvent rate       : {rate:.3f}\n\n"

            info += f"\tTime start       : {self.observation_time_start}\n"
            info += f"\tTime stop        : {self.observation_time_stop}\n\n"

        info += f"\tMin. energy      : {np.min(self.energy):.2e}\n"
        info += f"\tMax. energy      : {np.max(self.energy):.2e}\n"
        info += f"\tMedian energy    : {np.median(self.energy):.2e}\n\n"

        if self.is_pointed_observation:
            offset_max = np.max(self.offset)
            info += f"\tMax. offset      : {offset_max:.1f}\n"
        return info.expandtabs(tabsize=2)

    @property
    def time_ref(self):
        """Time reference as a `~astropy.time.Time` object."""
        return time_ref_from_dict(self.table.meta)

    @property
    def time(self):
        """Event times as a `~astropy.time.Time` object.

        Notes
        -----
        Times are automatically converted to 64-bit floats.
        With 32-bit floats times will be incorrect by a few seconds
        when e.g. adding them to the reference time.
        """
        met = u.Quantity(self.table["TIME"].astype("float64"), "second")
        return self.time_ref + met

    @property
    def observation_time_start(self):
        """Observation start time as a `~astropy.time.Time` object."""
        return self.time_ref + u.Quantity(self.table.meta["TSTART"], "second")

    @property
    def observation_time_stop(self):
        """Observation stop time as a `~astropy.time.Time` object."""
        return self.time_ref + u.Quantity(self.table.meta["TSTOP"], "second")

    @property
    def radec(self):
        """Event RA / DEC sky coordinates as a `~astropy.coordinates.SkyCoord` object."""
        lon, lat = self.table["RA"], self.table["DEC"]
        return SkyCoord(lon, lat, unit="deg", frame="icrs")

    @property
    def galactic(self):
        """Event Galactic sky coordinates as a `~astropy.coordinates.SkyCoord` object.

        Always computed from RA / DEC using Astropy.
        """
        return self.radec.galactic

    @property
    def energy(self):
        """Event energies as a `~astropy.units.Quantity`."""
        return self.table["ENERGY"].quantity

    @property
    def galactic_median(self):
        """Median position as a `~astropy.coordinates.SkyCoord` object."""
        galactic = self.galactic
        median_lon = np.median(galactic.l.wrap_at("180d"))
        median_lat = np.median(galactic.b)
        return SkyCoord(median_lon, median_lat, frame="galactic")

    def select_row_subset(self, row_specifier):
        """Select table row subset.

        Parameters
        ----------
        row_specifier : slice or int or array of int
            Specification for rows to select,
            passed to ``self.table[row_specifier]``.

        Returns
        -------
        event_list : `EventList`
            New event list with table row subset selected.

        Examples
        --------
        >>> from gammapy.data import EventList
        >>> import numpy as np
        >>> filename = "$GAMMAPY_DATA/cta-1dc/data/baseline/gps/gps_baseline_110380.fits"
        >>> events = EventList.read(filename)
        >>> #Use a boolean mask as ``row_specifier``:
        >>> mask = events.table['MC_ID'] == 1
        >>> events2 = events.select_row_subset(mask)
        >>> print(len(events2.table))
        97978
        >>> #Use row index array as ``row_specifier``:
        >>> idx = np.where(events.table['MC_ID'] == 1)[0]
        >>> events2 = events.select_row_subset(idx)
        >>> print(len(events2.table))
        97978
        """
        table = self.table[row_specifier]
        return self.__class__(table=table)

    def select_energy(self, energy_range):
        """Select events in energy band.

        Parameters
        ----------
        energy_range : `~astropy.units.Quantity`
            Energy range ``[energy_min, energy_max)``.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.

        Examples
        --------
        >>> from astropy import units as u
        >>> from gammapy.data import EventList
        >>> filename = "$GAMMAPY_DATA/fermi_3fhl/fermi_3fhl_events_selected.fits.gz"
        >>> event_list = EventList.read(filename)
        >>> energy_range =[1, 20] * u.TeV
        >>> event_list = event_list.select_energy(energy_range=energy_range)
        """
        energy = self.energy
        mask = energy_range[0] <= energy
        mask &= energy < energy_range[1]
        return self.select_row_subset(mask)

    def select_time(self, time_interval):
        """Select events in time interval.

        Parameters
        ----------
        time_interval : `astropy.time.Time`
            Start time (inclusive) and stop time (exclusive) for the selection.

        Returns
        -------
        events : `EventList`
            Copy of event list with selection applied.
        """
        time = self.time
        mask = time_interval[0] <= time
        mask &= time < time_interval[1]
        return self.select_row_subset(mask)

    def select_region(self, regions, wcs=None):
        """Select events in given region.

        Parameters
        ----------
        regions : str or `~regions.Region` or list of `~regions.Region`
            Region or list of regions (pixel or sky regions accepted).
            A region can be defined as a string in the DS9 format as well.
            See http://ds9.si.edu/doc/ref/region.html for details.
        wcs : `~astropy.wcs.WCS`, optional
            World coordinate system transformation. Default is None.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.
        """
        geom = RegionGeom.from_regions(regions, wcs=wcs)
        mask = geom.contains(self.radec)
        return self.select_row_subset(mask)

    @deprecated_renamed_argument("band", "values", "2.0")
    def select_parameter(self, parameter, values, is_range=True):
        """
        Event selection according to parameter values, either in a range or exact matches.

        Parameters
        ----------
        parameter : str
            Column name to filter on.
        values : tuple, list or `~numpy.ndarray`
            Value(s) for the parameter to be selected on.
        is_range : `bool`, optional
            Treat as numerical range (min,max). Default is True.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.

        Examples
        --------
        >>> from astropy import units as u
        >>> from gammapy.data import EventList
        >>> filename = "$GAMMAPY_DATA/fermi_3fhl/fermi_3fhl_events_selected.fits.gz"
        >>> event_list = EventList.read(filename)
        >>> zd = (0, 30) * u.deg
        >>> # Select event list through the zenith angle
        >>> event_list_zd = event_list.select_parameter(parameter='ZENITH_ANGLE', values=zd)
        >>> print(len(event_list_zd.table))
        123944
        >>> # Select event list through the run ID
        >>> event_list_id = event_list.select_parameter(parameter='RUN_ID', values=[239557414, 239559565, 459941302], is_range=False)
        >>> print(len(event_list_id.table))
        38
        """
        col_data = self.table[parameter]

        if is_range:
            # Handle numerical range case
            if len(values) > 2:
                warnings.warn(
                    "More than two arguments were given while selecting a range, only the first two were used for events selection."
                )

            mask = (values[0] <= col_data) & (col_data < values[1])
        else:
            # Universal comparison that works for strings and numbers
            mask = np.zeros(len(col_data), dtype=bool)
            for value in values:
                if not isinstance(value, str) and np.isnan(value):
                    mask |= np.isnan(col_data.data.astype(float))
                else:
                    mask |= col_data == value  # Works for both strings and numbers

        return self.select_row_subset(mask)

    @property
    def _default_plot_energy_axis(self):
        energy = self.energy
        return MapAxis.from_energy_bounds(
            energy_min=energy.min(), energy_max=energy.max(), nbin=50
        )

    def plot_energy(self, ax=None, **kwargs):
        """Plot counts as a function of energy.

        Parameters
        ----------
        ax : `~matplotlib.axes.Axes`, optional
            Matplotlib axes. Default is None
        **kwargs : dict, optional
            Keyword arguments passed to `~matplotlib.pyplot.hist`.

        Returns
        -------
        ax : `~matplotlib.axes.Axes`
            Matplotlib axes.
        """
        ax = plt.gca() if ax is None else ax

        energy_axis = self._default_plot_energy_axis

        kwargs.setdefault("log", True)
        kwargs.setdefault("histtype", "step")
        kwargs.setdefault("bins", energy_axis.edges)

        with quantity_support():
            ax.hist(self.energy, **kwargs)

        energy_axis.format_plot_xaxis(ax=ax)
        ax.set_ylabel("Counts")
        ax.set_yscale("log")
        return ax

    def plot_time(self, ax=None, **kwargs):
        """Plot an event rate time curve.

        Parameters
        ----------
        ax : `~matplotlib.axes.Axes`, optional
            Matplotlib axes. Default is None.
        **kwargs : dict, optional
            Keyword arguments passed to `~matplotlib.pyplot.errorbar`.

        Returns
        -------
        ax : `~matplotlib.axes.Axes`
            Matplotlib axes.
        """
        ax = plt.gca() if ax is None else ax

        # Note the events are not necessarily in time order
        time = self.table["TIME"]
        time = time - np.min(time)

        ax.set_xlabel(f"Time [{u.s.to_string(UNIT_STRING_FORMAT)}]")
        ax.set_ylabel("Counts")
        y, x_edges = np.histogram(time, bins=20)

        xerr = np.diff(x_edges) / 2
        x = x_edges[:-1] + xerr
        yerr = np.sqrt(y)

        kwargs.setdefault("fmt", "none")

        ax.errorbar(x=x, y=y, xerr=xerr, yerr=yerr, **kwargs)

        return ax

    def plot_offset2_distribution(
        self, ax=None, center=None, max_percentile=98, **kwargs
    ):
        """Plot offset^2 distribution of the events.

        The distribution shown in this plot is for this quantity::

            offset = center.separation(events.radec).deg
            offset2 = offset ** 2

        Note that this method is just for a quicklook plot.

        If you want to do computations with the offset or offset^2 values, you can
        use the line above. As an example, here's how to compute the 68% event
        containment radius using `numpy.percentile`::

            import numpy as np
            r68 = np.percentile(offset, q=68)

        Parameters
        ----------
        ax : `~matplotlib.axes.Axes`, optional
            Matplotlib axes. Default is None.
        center : `astropy.coordinates.SkyCoord`, optional
            Center position for the offset^2 distribution.
            Default is the observation pointing position.
        max_percentile : float, optional
            Define the percentile of the offset^2 distribution used to define the maximum offset^2 value.
            Default is 98.
        **kwargs : dict, optional
            Extra keyword arguments are passed to `~matplotlib.pyplot.hist`.

        Returns
        -------
        ax : `~matplotlib.axes.Axes`
            Matplotlib axes.

        Examples
        --------
        Load an example event list:

        >>> from gammapy.data import EventList
        >>> from astropy import units as u
        >>> filename = "$GAMMAPY_DATA/hess-dl3-dr1/data/hess_dl3_dr1_obs_id_023523.fits.gz"
        >>> events = EventList.read(filename)

        >>> #Plot the offset^2 distribution wrt. the observation pointing position
        >>> #(this is a commonly used plot to check the background spatial distribution):
        >>> events.plot_offset2_distribution() # doctest: +SKIP
        Plot the offset^2 distribution wrt. the Crab pulsar position (this is
        commonly used to check both the gamma-ray signal and the background
        spatial distribution):

        >>> import numpy as np
        >>> from astropy.coordinates import SkyCoord
        >>> center = SkyCoord(83.63307, 22.01449, unit='deg')
        >>> bins = np.linspace(start=0, stop=0.3 ** 2, num=30) * u.deg ** 2
        >>> events.plot_offset2_distribution(center=center, bins=bins) # doctest: +SKIP

        Note how we passed the ``bins`` option of `matplotlib.pyplot.hist` to control
        the histogram binning, in this case 30 bins ranging from 0 to (0.3 deg)^2.
        """
        ax = plt.gca() if ax is None else ax

        if center is None:
            center = self._plot_center

        offset2 = center.separation(self.radec) ** 2
        max2 = np.percentile(offset2, q=max_percentile)

        kwargs.setdefault("histtype", "step")
        kwargs.setdefault("bins", 30)
        kwargs.setdefault("range", (0.0, max2.value))

        with quantity_support():
            ax.hist(offset2, **kwargs)

        ax.set_xlabel(rf"Offset$^2$ [{ax.xaxis.units.to_string(UNIT_STRING_FORMAT)}]")
        ax.set_ylabel("Counts")
        return ax

    def plot_energy_offset(self, ax=None, center=None, **kwargs):
        """Plot counts histogram with energy and offset axes.

        Parameters
        ----------
        ax : `~matplotlib.pyplot.Axis`, optional
            Plot axis. Default is None.
        center : `~astropy.coordinates.SkyCoord`, optional
            Sky coord from which offset is computed. Default is None.
        **kwargs : dict, optional
            Keyword arguments forwarded to `~matplotlib.pyplot.pcolormesh`.

        Returns
        -------
        ax : `~matplotlib.pyplot.Axis`
            Plot axis.
        """
        from matplotlib.colors import LogNorm

        ax = plt.gca() if ax is None else ax

        if center is None:
            center = self._plot_center

        energy_axis = self._default_plot_energy_axis

        offset = center.separation(self.radec)
        offset_axis = MapAxis.from_bounds(
            0 * u.deg, offset.max(), nbin=30, name="offset"
        )

        counts = np.histogram2d(
            x=self.energy,
            y=offset,
            bins=(energy_axis.edges, offset_axis.edges),
        )[0]

        kwargs.setdefault("norm", LogNorm())

        with quantity_support():
            ax.pcolormesh(energy_axis.edges, offset_axis.edges, counts.T, **kwargs)

        energy_axis.format_plot_xaxis(ax=ax)
        offset_axis.format_plot_yaxis(ax=ax)
        return ax

    def check(self, checks="all"):
        """Run checks.

        This is a generator that yields a list of dicts.
        """
        checker = EventListChecker(self)
        return checker.run(checks=checks)

    def map_coord(self, geom):
        """Event map coordinates for a given geometry.

        Parameters
        ----------
        geom : `~gammapy.maps.Geom`
            Geometry.

        Returns
        -------
        coord : `~gammapy.maps.MapCoord`
            Coordinates.
        """
        coord = {"skycoord": self.radec}

        cols = {k.upper(): v for k, v in self.table.columns.items()}

        for axis in geom.axes:
            try:
                col = cols[axis.name.upper()]
                coord[axis.name] = u.Quantity(col).to(axis.unit)
            except KeyError:
                raise KeyError(f"Column not found in event list: {axis.name!r}")

        return MapCoord.create(coord)

    def select_mask(self, mask):
        """Select events inside a mask (`EventList`).

        Parameters
        ----------
        mask : `~gammapy.maps.Map`
            Mask.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.

        Examples
        --------
        >>> from gammapy.data import EventList
        >>> from gammapy.maps import WcsGeom, Map
        >>> geom = WcsGeom.create(skydir=(0,0), width=(4, 4), frame="galactic")
        >>> mask = geom.region_mask("galactic;circle(0, 0, 0.5)")
        >>> filename = "$GAMMAPY_DATA/cta-1dc/data/baseline/gps/gps_baseline_110380.fits"
        >>> events = EventList.read(filename)
        >>> masked_event = events.select_mask(mask)
        >>> len(masked_event.table)
        5594
        """
        coord = self.map_coord(mask.geom)
        values = mask.get_by_coord(coord)
        valid = values > 0
        return self.select_row_subset(valid)

    @property
    def observatory_earth_location(self):
        """Observatory location as an `~astropy.coordinates.EarthLocation` object."""
        return earth_location_from_dict(self.table.meta)

    @property
    def observation_time_duration(self):
        """Observation time duration in seconds as a `~astropy.units.Quantity`.

        This is a keyword related to IACTs.
        The wall time, including dead-time.
        """
        time_delta = (self.observation_time_stop - self.observation_time_start).sec
        return u.Quantity(time_delta, "s")

    @property
    def observation_live_time_duration(self):
        """Live-time duration in seconds as a `~astropy.units.Quantity`.

        The dead-time-corrected observation time.

        - In Fermi-LAT it is automatically provided in the header of the event list.
        - In IACTs is computed as ``t_live = t_observation * (1 - f_dead)`` where ``f_dead`` is the dead-time fraction.
        """
        return u.Quantity(self.table.meta["LIVETIME"], "second")

    @property
    def observation_dead_time_fraction(self):
        """Dead-time fraction as a float.

        This is a keyword related to IACTs.
        Defined as dead-time over observation time.

        Dead-time is defined as the time during the observation
        where the detector didn't record events:
        http://en.wikipedia.org/wiki/Dead_time
        https://ui.adsabs.harvard.edu/abs/2004APh....22..285F

        The dead-time fraction is used in the live-time computation,
        which in turn is used in the exposure and flux computation.
        """
        return 1 - self.table.meta["DEADC"]

    @property
    def altaz_frame(self):
        """ALT / AZ frame as an `~astropy.coordinates.AltAz` object."""
        return AltAz(obstime=self.time, location=self.observatory_earth_location)

    @property
    def altaz(self):
        """ALT / AZ position computed from RA / DEC as a `~astropy.coordinates.SkyCoord` object."""
        return self.radec.transform_to(self.altaz_frame)

    @property
    def altaz_from_table(self):
        """ALT / AZ position from table as a `~astropy.coordinates.SkyCoord` object."""
        lon = self.table["AZ"]
        lat = self.table["ALT"]
        return SkyCoord(lon, lat, unit="deg", frame=self.altaz_frame)

    @property
    def pointing_radec(self):
        """Pointing RA / DEC sky coordinates as a `~astropy.coordinates.SkyCoord` object."""
        info = self.table.meta
        lon, lat = info["RA_PNT"], info["DEC_PNT"]
        return SkyCoord(lon, lat, unit="deg", frame="icrs")

    @property
    def offset(self):
        """Event offset from the array pointing position as an `~astropy.coordinates.Angle`."""
        position = self.radec
        center = self.pointing_radec
        offset = center.separation(position)
        return Angle(offset, unit="deg")

    @property
    def offset_from_median(self):
        """Event offset from the median position as an `~astropy.coordinates.Angle`."""
        position = self.radec
        center = self.galactic_median
        offset = center.separation(position)
        return Angle(offset, unit="deg")

    def select_offset(self, offset_band):
        """Select events in offset band.

        Parameters
        ----------
        offset_band : `~astropy.coordinates.Angle`
            offset band ``[offset_min, offset_max)``.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.

        Examples
        --------
        >>> from gammapy.data import EventList
        >>> import astropy.units as u
        >>> filename = "$GAMMAPY_DATA/cta-1dc/data/baseline/gps/gps_baseline_110380.fits"
        >>> events = EventList.read(filename)
        >>> selected_events = events.select_offset([0.3, 0.9]*u.deg)
        >>> len(selected_events.table)
        12688

        """
        offset = self.offset
        mask = offset_band[0] <= offset
        mask &= offset < offset_band[1]
        return self.select_row_subset(mask)

    def select_rad_max(self, rad_max, position=None):
        """Select energy dependent offset.

        Parameters
        ----------
        rad_max : `~gamapy.irf.RadMax2D`
            Rad max definition.
        position : `~astropy.coordinates.SkyCoord`, optional
            Center position. Default is the pointing position.

        Returns
        -------
        event_list : `EventList`
            Copy of event list with selection applied.
        """
        if position is None:
            position = self.pointing_radec

        offset = position.separation(self.pointing_radec)
        separation = position.separation(self.radec)

        rad_max_for_events = rad_max.evaluate(
            method="nearest", energy=self.energy, offset=offset
        )

        selected = separation <= rad_max_for_events
        return self.select_row_subset(selected)

    @property
    def is_pointed_observation(self):
        """Whether observation is pointed."""
        return "RA_PNT" in self.table.meta

    def peek(self, allsky=False):
        """Quick look plots.

        This method creates a figure with five subplots for ``allsky=False``:

        * 2D counts map
        * Offset squared distribution of the events
        * Counts 2D histogram plot : full field of view offset versus energy
        * Counts spectrum plot : counts as a function of energy
        * Event rate time plot : counts as a function of time

        If ``allsky=True`` the first three subplots are replaced by an all-sky map of the counts.


        Parameters
        ----------
        allsky : bool, optional
            Whether to look at the events all-sky. Default is False.
        """
        import matplotlib.gridspec as gridspec

        if allsky:
            gs = gridspec.GridSpec(nrows=2, ncols=2)
            fig = plt.figure(figsize=(8, 8))
        else:
            gs = gridspec.GridSpec(nrows=2, ncols=3)
            fig = plt.figure(figsize=(12, 8))

        # energy plot
        ax_energy = fig.add_subplot(gs[1, 0])
        self.plot_energy(ax=ax_energy)

        # offset plots
        if not allsky:
            ax_offset = fig.add_subplot(gs[0, 1])
            self.plot_offset2_distribution(ax=ax_offset)
            ax_energy_offset = fig.add_subplot(gs[0, 2])
            self.plot_energy_offset(ax=ax_energy_offset)

        # time plot
        ax_time = fig.add_subplot(gs[1, 1])
        self.plot_time(ax=ax_time)

        # image plot
        m = self._counts_image(allsky=allsky)
        if allsky:
            ax_image = fig.add_subplot(gs[0, :], projection=m.geom.wcs)
        else:
            ax_image = fig.add_subplot(gs[0, 0], projection=m.geom.wcs)
        m.plot(ax=ax_image, stretch="sqrt", vmin=0, add_cbar=True)
        plt.subplots_adjust(wspace=0.3)

    @property
    def _plot_center(self):
        if self.is_pointed_observation:
            return self.pointing_radec
        else:
            return self.galactic_median

    @property
    def _plot_width(self):
        if self.is_pointed_observation:
            offset = self.offset
        else:
            offset = self.offset_from_median

        return 2 * offset.max()

    def _counts_image(self, allsky):
        if allsky:
            opts = {
                "npix": (360, 180),
                "binsz": 1.0,
                "proj": "AIT",
                "frame": "galactic",
            }
        else:
            opts = {
                "width": self._plot_width,
                "binsz": 0.05,
                "proj": "TAN",
                "frame": "galactic",
                "skydir": self._plot_center,
            }

        m = WcsNDMap.create(**opts)
        m.fill_by_coord(self.radec)
        m = m.smooth(width=0.5)
        return m

    def plot_image(self, ax=None, allsky=False):
        """Quick look counts map sky plot.

        Parameters
        ----------
        ax : `~matplotlib.pyplot.Axes`, optional
            Matplotlib axes.
        allsky :  bool, optional
            Whether to plot on an all sky geom. Default is False.
        """
        if ax is None:
            ax = plt.gca()
        m = self._counts_image(allsky=allsky)
        m.plot(ax=ax, stretch="sqrt")

    def copy(self):
        """Copy event list (`EventList`)."""
        return copy.deepcopy(self)


class EventListChecker(Checker):
    """Event list checker.

    Data format specification: ref:`gadf:iact-events`.

    Parameters
    ----------
    event_list : `~gammapy.data.EventList`
        Event list.
    """

    CHECKS = {
        "meta": "check_meta",
        "columns": "check_columns",
        "times": "check_times",
        "coordinates_galactic": "check_coordinates_galactic",
        "coordinates_altaz": "check_coordinates_altaz",
    }

    accuracy = {"angle": Angle("1 arcsec"), "time": u.Quantity(1, "microsecond")}

    # https://gamma-astro-data-formats.readthedocs.io/en/latest/events/events.html#mandatory-header-keywords  # noqa: E501
    meta_required = [
        "HDUCLASS",
        "HDUDOC",
        "HDUVERS",
        "HDUCLAS1",
        "OBS_ID",
        "TSTART",
        "TSTOP",
        "ONTIME",
        "LIVETIME",
        "DEADC",
        "RA_PNT",
        "DEC_PNT",
        # TODO: what to do about these?
        # They are currently listed as required in the spec,
        # but I think we should just require ICRS and those
        # are irrelevant, should not be used.
        # 'RADECSYS',
        # 'EQUINOX',
        "ORIGIN",
        "TELESCOP",
        "INSTRUME",
        "CREATOR",
        # https://gamma-astro-data-formats.readthedocs.io/en/latest/general/time.html#time-formats  # noqa: E501
        "MJDREFI",
        "MJDREFF",
        "TIMEUNIT",
        "TIMESYS",
        "TIMEREF",
        # https://gamma-astro-data-formats.readthedocs.io/en/latest/general/coordinates.html#coords-location  # noqa: E501
        "GEOLON",
        "GEOLAT",
        "ALTITUDE",
    ]

    _col = collections.namedtuple("col", ["name", "unit"])
    columns_required = [
        _col(name="EVENT_ID", unit=""),
        _col(name="TIME", unit="s"),
        _col(name="RA", unit="deg"),
        _col(name="DEC", unit="deg"),
        _col(name="ENERGY", unit="TeV"),
    ]

    def __init__(self, event_list):
        self.event_list = event_list

    def _record(self, level="info", msg=None):
        obs_id = self.event_list.table.meta["OBS_ID"]
        return {"level": level, "obs_id": obs_id, "msg": msg}

    def check_meta(self):
        meta_missing = sorted(set(self.meta_required) - set(self.event_list.table.meta))
        if meta_missing:
            yield self._record(
                level="error", msg=f"Missing meta keys: {meta_missing!r}"
            )

    def check_columns(self):
        t = self.event_list.table

        if len(t) == 0:
            yield self._record(level="error", msg="Events table has zero rows")

        for name, unit in self.columns_required:
            if name not in t.colnames:
                yield self._record(level="error", msg=f"Missing table column: {name!r}")
            else:
                if u.Unit(unit) != (t[name].unit or ""):
                    yield self._record(
                        level="error", msg=f"Invalid unit for column: {name!r}"
                    )

    def check_times(self):
        dt = (self.event_list.time - self.event_list.observation_time_start).sec
        if dt.min() < self.accuracy["time"].to_value("s"):
            yield self._record(level="error", msg="Event times before obs start time")

        dt = (self.event_list.time - self.event_list.observation_time_stop).sec
        if dt.max() > self.accuracy["time"].to_value("s"):
            yield self._record(level="error", msg="Event times after the obs end time")

        if np.min(np.diff(dt)) <= 0:
            yield self._record(level="error", msg="Events are not time-ordered.")

    def check_coordinates_galactic(self):
        """Check if RA / DEC matches GLON / GLAT."""
        t = self.event_list.table

        if "GLON" not in t.colnames:
            return

        galactic = SkyCoord(t["GLON"], t["GLAT"], unit="deg", frame="galactic")
        separation = self.event_list.radec.separation(galactic).to("arcsec")
        if separation.max() > self.accuracy["angle"]:
            yield self._record(
                level="error", msg="GLON / GLAT not consistent with RA / DEC"
            )

    def check_coordinates_altaz(self):
        """Check if ALT / AZ matches RA / DEC."""
        t = self.event_list.table

        if "AZ" not in t.colnames:
            return

        altaz_astropy = self.event_list.altaz
        separation = angular_separation(
            altaz_astropy.data.lon,
            altaz_astropy.data.lat,
            t["AZ"].quantity,
            t["ALT"].quantity,
        )
        if separation.max() > self.accuracy["angle"]:
            yield self._record(
                level="error", msg="ALT / AZ not consistent with RA / DEC"
            )
