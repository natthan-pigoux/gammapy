# Licensed under a 3-clause BSD style license - see LICENSE.rst
import numpy as np
from gammapy.maps import Map, MapAxis, MapCoord, RegionGeom, WcsGeom
from gammapy.utils.random import InverseCDFSampler, get_random_state
from ..core import IRFMap
from .kernel import EDispKernel

__all__ = ["EDispMap", "EDispKernelMap"]


def get_overlap_fraction(energy_axis, energy_axis_true):
    a_min = energy_axis.edges[:-1]
    a_max = energy_axis.edges[1:]

    b_min = energy_axis_true.edges[:-1][:, np.newaxis]
    b_max = energy_axis_true.edges[1:][:, np.newaxis]

    xmin = np.fmin(a_max, b_max)
    xmax = np.fmax(a_min, b_min)
    return (np.clip(xmin - xmax, 0, np.inf) / (b_max - b_min)).to("")


class EDispMap(IRFMap):
    """Energy dispersion map.

    Parameters
    ----------
    edisp_map : `~gammapy.maps.Map`
        The input Energy Dispersion Map. Should be a Map with 2 non-spatial axes.
        migra and true energy axes should be given in this specific order.
    exposure_map : `~gammapy.maps.Map`, optional
        Associated exposure map. Needs to have a consistent map geometry.

    Examples
    --------
    ::

        # Energy dispersion map for CTAO data
        import numpy as np
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        from gammapy.maps import WcsGeom, MapAxis
        from gammapy.irf import EnergyDispersion2D, EffectiveAreaTable2D
        from gammapy.makers.utils import make_edisp_map, make_map_exposure_true_energy

        # Define energy dispersion map geometry
        energy_axis_true = MapAxis.from_edges(np.logspace(-1, 1, 10), unit="TeV", name="energy_true")
        migra_axis = MapAxis.from_edges(np.linspace(0, 3, 100), name="migra")
        pointing = SkyCoord(0, 0, unit="deg")
        geom = WcsGeom.create(
                binsz=0.25 * u.deg,
                width=10 * u.deg,
                skydir=pointing,
                axes=[migra_axis, energy_axis_true],
        )

        # Extract EnergyDispersion2D from CTA 1DC IRF
        filename = "$GAMMAPY_DATA/cta-1dc/caldb/data/cta/1dc/bcf/South_z20_50h/irf_file.fits"
        edisp2D = EnergyDispersion2D.read(filename, hdu="ENERGY DISPERSION")
        aeff2d = EffectiveAreaTable2D.read(filename, hdu="EFFECTIVE AREA")

        # Create the exposure map
        exposure_geom = geom.squash(axis_name="migra")
        exposure_map = make_map_exposure_true_energy(pointing, "1 h", aeff2d, exposure_geom)

        # Create the EDispMap for the specified pointing
        edisp_map = make_edisp_map(edisp2D, pointing, geom, exposure_map)

        # Get an Energy Dispersion (1D) at any position in the image
        pos = SkyCoord(2.0, 2.5, unit="deg")
        energy_axis = MapAxis.from_energy_bounds(0.1, 10, 5, unit="TeV", name="energy")
        edisp = edisp_map.get_edisp_kernel(energy_axis, position=pos)

        # Write map to disk
        edisp_map.write("edisp_map.fits")

    """

    tag = "edisp_map"
    required_axes = ["migra", "energy_true"]

    def __init__(self, edisp_map, exposure_map=None):
        super().__init__(irf_map=edisp_map, exposure_map=exposure_map)

    @property
    def edisp_map(self):
        return self._irf_map

    @edisp_map.setter
    def edisp_map(self, value):
        del self.has_single_spatial_bin
        self._irf_map = value

    def normalize(self):
        """Normalize PSF map."""
        self.edisp_map.normalize(axis_name="migra")

    def get_edisp_kernel(self, energy_axis, position=None):
        """Get energy dispersion at a given position.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            Reconstructed energy axis.
        position : `~astropy.coordinates.SkyCoord`
            The target position. Should be a single coordinates.

        Returns
        -------
        edisp : `~gammapy.irf.EnergyDispersion`
            The energy dispersion (i.e. rmf object).
        """
        edisp_map = self.to_region_nd_map(region=position)
        edisp_kernel_map = edisp_map.to_edisp_kernel_map(energy_axis=energy_axis)
        return edisp_kernel_map.get_edisp_kernel()

    def to_edisp_kernel_map(self, energy_axis):
        """Convert to map with energy dispersion kernels.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            Reconstructed energy axis.

        Returns
        -------
        edisp : `~gammapy.maps.EDispKernelMap`
            Energy dispersion kernel map.
        """
        energy_axis_true = self.edisp_map.geom.axes["energy_true"]

        geom_image = self.edisp_map.geom.to_image()
        geom = geom_image.to_cube([energy_axis, energy_axis_true])

        coords = geom.get_coord(sparse=True, mode="edges", axis_name="energy")

        migra = coords["energy"] / coords["energy_true"]

        coords = {
            "skycoord": coords.skycoord,
            "energy_true": coords["energy_true"],
            "migra": migra,
        }

        values = self.edisp_map.integral(axis_name="migra", coords=coords)

        axis = self.edisp_map.geom.axes.index_data("migra")
        data = np.clip(np.diff(values, axis=axis), 0, np.inf)

        edisp_kernel_map = Map.from_geom(geom=geom, data=data.to_value(""), unit="")

        if self.exposure_map:
            geom = geom.squash(axis_name=energy_axis.name)
            exposure_map = self.exposure_map.copy(geom=geom)
        else:
            exposure_map = None

        return EDispKernelMap(
            edisp_kernel_map=edisp_kernel_map, exposure_map=exposure_map
        )

    @classmethod
    def from_geom(cls, geom):
        """Create energy dispersion map from geometry.

        By default, a diagonal energy dispersion matrix is created.

        Parameters
        ----------
        geom : `~gammapy.maps.Geom`
            Energy dispersion map geometry.

        Returns
        -------
        edisp_map : `~gammapy.maps.EDispMap`
            Energy dispersion map.
        """
        if "energy_true" not in [ax.name for ax in geom.axes]:
            raise ValueError("EDispMap requires true energy axis")

        exposure_map = Map.from_geom(geom=geom.squash(axis_name="migra"), unit="m2 s")

        edisp_map = Map.from_geom(geom, unit="")
        migra_axis = geom.axes["migra"]
        migra_0 = migra_axis.coord_to_pix(1)

        # distribute over two pixels
        migra = geom.get_idx()[2]
        data = np.abs(migra - migra_0)
        data = np.where(data < 1, 1 - data, 0)
        edisp_map.quantity = data / migra_axis.bin_width.reshape((1, -1, 1, 1))
        return cls(edisp_map, exposure_map)

    def sample_coord(self, map_coord, random_state=0, chunk_size=10000):
        """Apply the energy dispersion corrections on the coordinates of a set of simulated events.

        Parameters
        ----------
        map_coord : `~gammapy.maps.MapCoord`
            Sequence of coordinates and energies of sampled events.
        random_state : {int, 'random-seed', 'global-rng', `~numpy.random.RandomState`}, optional
            Defines random number generator initialisation.
            Passed to `~gammapy.utils.random.get_random_state`.
            Default is 0.
        chunk_size : int
            If set, this will slice the input MapCoord into smaller chunks of chunk_size elements.
            Default is 10000.

        Returns
        -------
        `~gammapy.maps.MapCoord`.
            Sequence of energy dispersion corrected coordinates of the input map_coord map.
        """
        random_state = get_random_state(random_state)
        migra_axis = self.edisp_map.geom.axes["migra"]

        position = map_coord.skycoord
        energy_true = map_coord["energy_true"]

        size = position.size
        energy_reco = np.ones(size) * map_coord["energy_true"].unit
        chunk_size = size if chunk_size is None else chunk_size
        index = 0

        while index < size:
            chunk = slice(index, index + chunk_size, 1)
            coord = {
                "skycoord": position[chunk].reshape(-1, 1),
                "energy_true": energy_true[chunk].reshape(-1, 1),
                "migra": migra_axis.center,
            }

            pdf_edisp = self.edisp_map.interp_by_coord(coord)

            sample_edisp = InverseCDFSampler(
                pdf_edisp, axis=1, random_state=random_state
            )
            pix_edisp = sample_edisp.sample_axis()
            migra = migra_axis.pix_to_coord(pix_edisp)

            energy_reco[chunk] = energy_true[chunk] * migra
            index += chunk_size

        return MapCoord.create({"skycoord": position, "energy": energy_reco})

    @classmethod
    def from_diagonal_response(cls, energy_axis_true, migra_axis=None):
        """Create an all-sky EDisp map with diagonal response.

        Parameters
        ----------
        energy_axis_true : `~gammapy.maps.MapAxis`
            True energy axis.
        migra_axis : `~gammapy.maps.MapAxis`, optional
            Migra axis. Default is None.

        Returns
        -------
        edisp_map : `~gammapy.maps.EDispMap`
            Energy dispersion map.
        """
        migra_res = 1e-5
        migra_axis_default = MapAxis.from_bounds(
            1 - migra_res, 1 + migra_res, nbin=3, name="migra", node_type="edges"
        )

        migra_axis = migra_axis or migra_axis_default

        geom = WcsGeom.create(
            npix=(2, 1), proj="CAR", binsz=180, axes=[migra_axis, energy_axis_true]
        )

        return cls.from_geom(geom)

    def peek(self, figsize=(15, 5)):
        """Quick-look summary plots.

        Plots corresponding to the center of the map.

        This method creates a figure with two subplots:

        * Bias plot : reconstruction bias as a function of true energy
        * Energy dispersion matrix plot : probability density function matrix

        Parameters
        ----------
        figsize : tuple
            Size of figure.

        """
        e_true = self.edisp_map.geom.axes[1]
        e_reco = MapAxis.from_energy_bounds(
            e_true.edges.min(),
            e_true.edges.max(),
            nbin=len(e_true.center),
            name="energy",
        )

        self.get_edisp_kernel(energy_axis=e_reco).peek(figsize)


class EDispKernelMap(IRFMap):
    """Energy dispersion kernel map.

    Parameters
    ----------
    edisp_kernel_map : `~gammapy.maps.Map`
        The input energy dispersion kernel map. Should be a Map with 2 non-spatial axes.
        Reconstructed and true energy axes should be given in this specific order.
    exposure_map : `~gammapy.maps.Map`, optional
        Associated exposure map. Needs to have a consistent map geometry.

    """

    tag = "edisp_kernel_map"
    required_axes = ["energy", "energy_true"]

    def __init__(self, edisp_kernel_map, exposure_map=None):
        super().__init__(irf_map=edisp_kernel_map, exposure_map=exposure_map)

    @property
    def edisp_map(self):
        return self._irf_map

    @edisp_map.setter
    def edisp_map(self, value):
        self._irf_map = value

    @classmethod
    def from_geom(cls, geom):
        """Create energy dispersion map from geometry.

        By default, a diagonal energy dispersion matrix is created.

        Parameters
        ----------
        geom : `~gammapy.maps.Geom`
            Energy dispersion map geometry.

        Returns
        -------
        edisp_map : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        # TODO: allow only list of additional axes
        geom.axes.assert_names(cls.required_axes, allow_extra=True)
        geom_exposure = geom.squash(axis_name="energy")
        exposure = Map.from_geom(geom_exposure, unit="m2 s")

        energy_axis = geom.axes["energy"]
        energy_axis_true = geom.axes["energy_true"]

        data = get_overlap_fraction(energy_axis, energy_axis_true)

        edisp_kernel_map = Map.from_geom(geom, unit="")
        edisp_kernel_map.quantity += np.resize(data, geom.data_shape_axes)
        return cls(edisp_kernel_map=edisp_kernel_map, exposure_map=exposure)

    def get_edisp_kernel(self, position=None, energy_axis=None):
        """Get energy dispersion at a given position.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord` or `~regions.SkyRegion`, optional
            The target position. Should be a single coordinates.
            Default is None.
        energy_axis : `MapAxis`, optional
            Reconstructed energy axis, only used for checking.
            Default is None.

        Returns
        -------
        edisp : `~gammapy.irf.EnergyDispersion`
            The energy dispersion (i.e. rmf object).
        """
        if energy_axis:
            assert energy_axis == self.edisp_map.geom.axes["energy"]

        if isinstance(self.edisp_map.geom, RegionGeom):
            kernel_map = self.edisp_map
        else:
            if position is None:
                position = self.edisp_map.geom.center_skydir
            position = self._get_nearest_valid_position(position)

            kernel_map = self.edisp_map.to_region_nd_map(region=position)

        return EDispKernel(
            axes=kernel_map.geom.axes[["energy_true", "energy"]],
            data=kernel_map.data[..., 0, 0],
        )

    @classmethod
    def from_diagonal_response(cls, energy_axis, energy_axis_true, geom=None):
        """Create an energy dispersion map with diagonal response.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            Energy axis.
        energy_axis_true : `~gammapy.maps.MapAxis`
            True energy axis
        geom : `~gammapy.maps.Geom`, optional
            The (2D) geometry object to use. If None, an all sky geometry with 2 bins is created.
            Default is None.

        Returns
        -------
        edisp_map : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        if geom is None:
            geom = WcsGeom.create(
                npix=(2, 1), proj="CAR", binsz=180, axes=[energy_axis, energy_axis_true]
            )
        else:
            geom = geom.to_image().to_cube([energy_axis, energy_axis_true])

        return cls.from_geom(geom)

    @classmethod
    def from_edisp_kernel(cls, edisp, geom=None):
        """Create an energy dispersion map from the input 1D kernel.

        The kernel will be duplicated over all spatial bins.

        Parameters
        ----------
        edisp : `~gammapy.irf.EDispKernel`
            The input 1D kernel.
        geom : `~gammapy.maps.Geom`, optional
            The (2D) geometry object to use. If None, an all sky geometry with 2 bins is created.
            Default is None.

        Returns
        -------
        edisp_map : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        edisp_map = cls.from_diagonal_response(
            edisp.axes["energy"], edisp.axes["energy_true"], geom=geom
        )
        edisp_map.edisp_map.data *= 0
        edisp_map.edisp_map.data[:, :, ...] = edisp.pdf_matrix[
            :, :, np.newaxis, np.newaxis
        ]
        return edisp_map

    @classmethod
    def from_gauss(
        cls, energy_axis, energy_axis_true, sigma, bias, pdf_threshold=1e-6, geom=None
    ):
        """Create an energy dispersion map from the input 1D kernel.

        The kernel will be duplicated over all spatial bins.

        Parameters
        ----------
        energy_axis_true : `~astropy.units.Quantity`
            Bin edges of true energy axis.
        energy_axis : `~astropy.units.Quantity`
            Bin edges of reconstructed energy axis.
        bias : float or `~numpy.ndarray`
            Center of Gaussian energy dispersion, bias.
        sigma : float or `~numpy.ndarray`
            RMS width of Gaussian energy dispersion, resolution.
        pdf_threshold : float, optional
            Zero suppression threshold. Default is 1e-6.
        geom : `~gammapy.maps.Geom`, optional
            The (2D) geometry object to use. If None, an all sky geometry with 2 bins is created.
            Default is None.

        Returns
        -------
        edisp_map : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        kernel = EDispKernel.from_gauss(
            energy_axis=energy_axis,
            energy_axis_true=energy_axis_true,
            sigma=sigma,
            bias=bias,
            pdf_threshold=pdf_threshold,
        )
        return cls.from_edisp_kernel(kernel, geom=geom)

    def to_image(self, weights=None):
        """Return a 2D EdispKernelMap by summing over the reconstructed energy axis.

        Parameters
        ----------
        weights: `~gammapy.maps.Map`, optional
            Weights to be applied. Default is None.

        Returns
        -------
        edisp : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        edisp = self.edisp_map.data
        if weights:
            edisp = edisp * weights.data

        data = np.sum(edisp, axis=1, keepdims=True)
        geom = self.edisp_map.geom.squash(axis_name="energy")
        edisp_map = Map.from_geom(geom=geom, data=data)
        return self.__class__(
            edisp_kernel_map=edisp_map, exposure_map=self.exposure_map
        )

    def resample_energy_axis(self, energy_axis, weights=None):
        """Return a resampled `EDispKernelMap`.

        Bins are grouped according to the edges of the reconstructed energy axis provided.
        The true energy is left unchanged.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            The reconstructed energy axis to use for the grouping.
        weights: `~gammapy.maps.Map`, optional
            Weights to be applied. Default is None.

        Returns
        -------
        edisp : `EDispKernelMap`
            Energy dispersion kernel map.
        """
        new_edisp_map = self.edisp_map.resample_axis(axis=energy_axis, weights=weights)
        return self.__class__(
            edisp_kernel_map=new_edisp_map, exposure_map=self.exposure_map
        )

    def peek(self, figsize=(15, 5)):
        """Quick-look summary plots.

        Plots corresponding to the center of the map.

        This method creates a figure with two subplots:

        * Bias plot : reconstruction bias as a function of true energy
        * Energy dispersion matrix plot : probability density function matrix

        Parameters
        ----------
        figsize : tuple, optional
            Size of the figure. Default is (15, 5).

        """
        self.get_edisp_kernel().peek(figsize)

    @classmethod
    def read(cls, filename, format="gadf", hdu=None, checksum=False):
        """Read an EDispKernelMap from file and create corresponding object.

        Parameters
        ----------
        filename : str or `~pathlib.Path`
            File name.
        format : {"gadf", "gtdrm"}, optional
            File format. Default is "gadf".
            Format "gtdrm" creates an EDispKernelMap including a single EDispKernel.
        hdu : str or int
            HDU location. Default is None.
        checksum : bool
            If True checks both DATASUM and CHECKSUM cards in the file headers. Default is False.

        Returns
        -------
        edisp : `EDispKernelMap`
            Energy dispersion kernel map.

        """

        if format == "gadf":
            return super().read(filename, format, hdu, checksum)
        elif format == "gtdrm":
            edisp = EDispKernel.read(filename, format="gtdrm", checksum=checksum)
            geom_edisp = RegionGeom.create(region=None, axes=edisp.axes)
            return EDispKernelMap.from_edisp_kernel(edisp, geom=geom_edisp)
        else:
            raise ValueError(f"Unrecognized format: {format}")
