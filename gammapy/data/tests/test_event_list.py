# Licensed under a 3-clause BSD style license - see LICENSE.rst
import pytest
import numpy as np
from numpy.testing import assert_allclose
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table
from regions import CircleSkyRegion, RectangleSkyRegion
from gammapy.data import GTI, EventList, Observation, FixedPointingInfo
from gammapy.maps import MapAxis, WcsGeom
from gammapy.utils.testing import mpl_plot_check, requires_data


class TestEventListBasic:
    def setup_class(self):
        table = Table()
        table["RA"] = [0.0, 0.0, 0.0, 10.0] * u.deg
        table["DEC"] = [0.0, 0.9, 10.0, 10.0] * u.deg
        table["ENERGY"] = [1.0, 1.5, 1.5, 10.0] * u.TeV
        table["TIME"] = [0.1, 0.5, 1.0, 1.5] * u.second

        self.events = EventList(table)

    def test_eventlist_printin(self):
        print(self.events)


@requires_data()
class TestEventListBase:
    def setup_class(self):
        self.events = EventList.read(
            "$GAMMAPY_DATA/hess-dl3-dr1/data/hess_dl3_dr1_obs_id_020136.fits.gz"
        )

    def test_select_parameter(self):
        events = self.events.select_parameter("ENERGY", (0.8 * u.TeV, 5.0 * u.TeV))
        assert len(events.table) == 2716

        with pytest.warns(UserWarning):
            events = self.events.select_parameter(
                "ENERGY", (0.8, 5, 10) * u.TeV, is_range=True
            )
            assert len(events.table) == 2716

        events = self.events.select_parameter(
            "EVENT_ID", [1808181231761, 3594887627737, 3599182594792], is_range=False
        )
        assert len(events.table) == 3

        events = self.events.select_parameter("ENERGY", (0.8, np.inf) * u.TeV)
        assert len(events.table) == 3944

    def test_meta(self):
        assert self.events.meta.event_class == "std"
        assert self.events.meta.creation.creator == "SASH FITS::EventListWriter"
        assert self.events.meta.creation.date is None
        assert self.events.meta.creation.origin == "H.E.S.S. Collaboration"
        assert self.events.table["EVENT_ID"][0] == 1808181231761

    def test_write(self):
        # Without GTI and pointing
        obs = Observation(events=self.events)
        # Write function is through obs
        with pytest.raises(ValueError):
            obs.write("test.fits.gz", include_irfs=False, overwrite=True)

        pointing = FixedPointingInfo.from_fits_header(self.events.table.meta)
        obs = Observation(events=self.events, pointing=pointing)
        obs.write("test.fits.gz", include_irfs=False, overwrite=True)
        read_again = EventList.read("test.fits.gz")

        assert (self.events.table == read_again.table).all()
        assert read_again.table.meta["EXTNAME"] == "EVENTS"
        assert read_again.table.meta["HDUCLASS"] == "GADF"
        assert read_again.table.meta["HDUCLAS1"] == "EVENTS"

        # With GTI
        gti = GTI.read(
            "$GAMMAPY_DATA/hess-dl3-dr1/data/hess_dl3_dr1_obs_id_020136.fits.gz"
        )

        obs = Observation(events=self.events, gti=gti, pointing=pointing)
        obs.write("test.fits", overwrite=True)
        read_again_ev = EventList.read("test.fits")
        read_again_gti = GTI.read("test.fits")

        assert (self.events.table == read_again_ev.table).all()
        assert gti.table.meta == read_again_gti.table.meta
        assert_allclose(gti.table["START"].mjd, read_again_gti.table["START"].mjd)
        assert_allclose(gti.table["STOP"].mjd, read_again_gti.table["STOP"].mjd)

        # test that it won't work if gti is not a GTI
        with pytest.raises(AttributeError):
            obs = Observation(events=self.events, gti=gti.table, pointing=pointing)
            obs.write("test.fits", overwrite=True)

    def test_eventlist_hdu_creation_metadata(self):
        hdu = self.events.to_table_hdu(format="gadf")
        assert "CREATOR" in hdu.header
        assert "CREATED" in hdu.header
        assert hdu.header["CREATOR"] == "SASH FITS::EventListWriter"


@requires_data()
class TestEventListHESS:
    def setup_class(self):
        self.events = EventList.read(
            "$GAMMAPY_DATA/hess-dl3-dr1/data/hess_dl3_dr1_obs_id_020136.fits.gz"
        )

    def test_basics(self):
        assert "EventList" in str(self.events)

        assert self.events.is_pointed_observation

        assert len(self.events.table) == 11243
        assert self.events.time[0].iso == "2004-03-26 02:57:47.004"
        assert self.events.radec[0].to_string() == "229.239 -58.3417"
        assert self.events.galactic[0].to_string(precision=2) == "321.07 -0.69"
        assert self.events.altaz[0].to_string() == "193.338 53.258"
        assert_allclose(self.events.offset[0].value, 0.54000974, rtol=1e-5)

        energy = self.events.energy[0]
        assert energy.unit == "TeV"
        assert_allclose(energy.value, 0.55890286)

        lon, lat, height = self.events.observatory_earth_location.to_geodetic()
        assert lon.unit == "deg"
        assert_allclose(lon.value, 16.5002222222222)
        assert lat.unit == "deg"
        assert_allclose(lat.value, -23.2717777777778)
        assert height.unit == "m"
        assert_allclose(height.value, 1835)

    def test_observation_time_duration(self):
        dt = self.events.observation_time_duration
        assert dt.unit == "s"
        assert_allclose(dt.value, 1682)

    def test_observation_live_time_duration(self):
        dt = self.events.observation_live_time_duration
        assert dt.unit == "s"
        assert_allclose(dt.value, 1521.026855)

    def test_observation_dead_time_fraction(self):
        deadc = self.events.observation_dead_time_fraction
        assert_allclose(deadc, 0.095703, rtol=1e-3)

    def test_altaz(self):
        altaz = self.events.altaz
        assert_allclose(altaz[0].az.deg, 193.337965, atol=1e-3)
        assert_allclose(altaz[0].alt.deg, 53.258024, atol=1e-3)

    def test_median_position(self):
        coord = self.events.galactic_median
        assert_allclose(coord.l.deg, 320.539346, atol=1e-3)
        assert_allclose(coord.b.deg, -0.882515, atol=1e-3)

    def test_median_offset(self):
        offset_max = self.events.offset_from_median.max()
        assert_allclose(offset_max.to_value("deg"), 36.346379, atol=1e-3)

    def test_from_stack(self):
        event_lists = [self.events] * 2
        stacked_list = EventList.from_stack(event_lists)
        assert len(stacked_list.table) == 11243 * 2

    def test_stack(self):
        events, other = self.events.copy(), self.events.copy()
        events.stack(other)
        assert len(events.table) == 11243 * 2

    def test_offset_selection(self):
        offset_range = u.Quantity([0.5, 1.0] * u.deg)
        new_list = self.events.select_offset(offset_range)
        assert len(new_list.table) == 1820

    def test_plot_time(self):
        with mpl_plot_check():
            self.events.plot_time()

    def test_plot_energy(self):
        with mpl_plot_check():
            self.events.plot_energy()

    def test_plot_offset2_distribution(self):
        with mpl_plot_check():
            self.events.plot_offset2_distribution()

    def test_plot_energy_offset(self):
        with mpl_plot_check():
            self.events.plot_energy_offset()

    def test_plot_image(self):
        with mpl_plot_check():
            self.events.plot_image()

    def test_peek(self):
        with mpl_plot_check():
            self.events.peek()


@requires_data()
class TestEventListFermi:
    def setup_class(self):
        self.events = EventList.read(
            "$GAMMAPY_DATA/fermi-3fhl-gc/fermi-3fhl-gc-events.fits.gz"
        )

    def test_basics(self):
        assert "EventList" in str(self.events)
        assert len(self.events.table) == 32843
        assert not self.events.is_pointed_observation

    def test_peek(self):
        with mpl_plot_check():
            self.events.peek(allsky=True)


@requires_data()
class TestEventListChecker:
    def setup_class(self):
        self.event_list = EventList.read(
            "$GAMMAPY_DATA/cta-1dc/data/baseline/gps/gps_baseline_111140.fits"
        )

    def test_check_all(self):
        records = list(self.event_list.check())
        assert len(records) == 3


class TestEventSelection:
    def setup_class(self):
        table = Table()
        table["RA"] = [0.0, 0.0, 0.0, 10.0] * u.deg
        table["DEC"] = [0.0, 0.9, 10.0, 10.0] * u.deg
        table["ENERGY"] = [1.0, 1.5, 1.5, 10.0] * u.TeV
        table["TIME"] = [0.1, 0.5, 1.0, 1.5] * u.second

        self.events = EventList(table)

        center1 = SkyCoord(0.0, 0.0, frame="icrs", unit="deg")
        on_region1 = CircleSkyRegion(center1, radius=1.0 * u.deg)
        center2 = SkyCoord(0.0, 10.0, frame="icrs", unit="deg")
        on_region2 = RectangleSkyRegion(center2, width=0.5 * u.deg, height=0.3 * u.deg)
        self.on_regions = [on_region1, on_region2]

    def test_region_select(self):
        geom = WcsGeom.create(skydir=(0, 0), binsz=0.2, width=4.0 * u.deg, proj="TAN")
        new_list = self.events.select_region(self.on_regions[0], geom.wcs)
        assert len(new_list.table) == 2

        union_region = self.on_regions[0].union(self.on_regions[1])
        new_list = self.events.select_region(union_region, geom.wcs)
        assert len(new_list.table) == 3

        region_string = "fk5;box(0,10, 0.25, 0.15)"
        new_list = self.events.select_region(region_string, geom.wcs)
        assert len(new_list.table) == 1

    def test_map_select(self):
        axis = MapAxis.from_edges((0.5, 2.0), unit="TeV", name="ENERGY")
        geom = WcsGeom.create(
            skydir=(0, 0), binsz=0.2, width=4.0 * u.deg, proj="TAN", axes=[axis]
        )

        mask = geom.region_mask(regions=[self.on_regions[0]])
        new_list = self.events.select_mask(mask)
        assert len(new_list.table) == 2

    def test_select_energy(self):
        energy_range = u.Quantity([1, 10], "TeV")
        new_list = self.events.select_energy(energy_range)
        assert len(new_list.table) == 3
