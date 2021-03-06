"""Defines corrections for interferometric errors."""

import numpy as np
from numba import jit
import numba
from skimage.util import view_as_windows
import logging
import itertools
from multiprocessing.pool import Pool
import sys

from .util import trapz, interp1d_jit
from .insar import SAR
from .era import ERAModel
from .geogrid import GeoGrid


def optim_era_delay(dem, ifg, mmodel, smodel, mwr, swr, min_plevel=200,
                    look_angle=0.367):
    helper_args = []
    max_plevel_idx = 0
    try:
        min_plevel_idx = np.nonzero(mmodel.pressure[0, 0, :] == min_plevel)[0][0]
    except IndexError:
        raise IndexError('Minimum pressure level could not be found in the '
                         'weather model')

    plevels = mmodel.pressure[0, 0, max_plevel_idx:min_plevel_idx + 1]
    test_plevels = itertools.product(plevels, plevels)

    for (master_p, slave_p) in test_plevels:
        helper_args += [(dem, ifg, mmodel, smodel, mwr, swr, master_p, slave_p,
                         look_angle)]

    logging.debug('%d pressure combinations to test', len(helper_args))
    with Pool() as p:
        results = p.starmap(_optim_era_delay, helper_args)

    return results


def _optim_era_delay(dem, ifg, mmodel, smodel, mwr, swr, master_min_plevel,
                     slave_min_plevel, look_angle=0.367):
    """Returns a dictionary with the keys 'master_p', 'slave_p' and
    'std'. 'master_p' and 'slave_p' are the minimum pressure levels used. 'std'
    is the standard deviation of the corrected interferogram."""
    # logging.debug('Testing master/slave pressure combination %.1f/%.1f',
    #               master_min_plevel, slave_min_plevel)
    mmodel.add_rainfall(mwr.data, master_min_plevel, 1000, 10)
    smodel.add_rainfall(swr.data, slave_min_plevel, 1000, 10)

    mwet, mdry, _ = calculate_era_zenith_delay(mmodel, dem)
    swet, sdry, _ = calculate_era_zenith_delay(smodel, dem)

    mwet, mdry = mwet.zenith2slant(look_angle), mdry.zenith2slant(look_angle)
    swet, sdry = swet.zenith2slant(look_angle), sdry.zenith2slant(look_angle)

    ifg_total = (mwet.data + mdry.data) - (swet.data + sdry.data)
    corrected_ifg = ifg.data - ifg_total

    std = corrected_ifg.std()
    print('{:4.1f}\t{:4.1f}\t{:2.5f}'.format(master_min_plevel, slave_min_plevel, std))
    sys.stdout.flush()

    return {
        'master_p': master_min_plevel,
        'slave_p': slave_min_plevel,
        'std': std
    }


def patches_era_delay(dem, ifg, mmodel, smodel, mwr, swr, min_plevel=200,
                      patch_size=(128, 128), look_angle=0.367):
    """Calculate interferometric delay using an advanced patching algorithm.

    Arguments
    ---------
    dem : geogrid.GeoGrid
      The DEM to use in calculations.
    ifg : insar.InSAR
      The interferogram to correct.
    mmodel : era.ERAModel
      Weather model for the master date.
    smodel : era.ERAModel
      Weather model for the slave date.
    mwr : nimrod.Nimrod
      Rainfall radar for the master date.
    swr : nimrod.Nimrod
      Rainfall radar for the slave date.
    min_plevel : float, opt
      The minimum pressure level to test. Default 200.
    patch_size : tuple, opt
      The size of patches to process in pixels. Default (128, 128) (lat,
      lon). This needs to divide evenly into the shape of the first two axes of
      the other parameters.
    look_angle : float | ndarray, opt
      The look angle of the satellite in radians. Default 0.367 (~21 deg).

    Returns
    -------
    mwet : insar.SAR
      The zenith wet delay on the master date.
    mdry : insar.SAR
      The zenith dry delay on the master date.
    swet : insar.SAR
      The zenith wet delay on the slave date.
    sdry : insar.SAR
      The zenith dry delay on the slave date.
    m_plevels, s_plevels : ndarray
      Matrices containing the minimum pressure level used at each pixel for the
      master and slave dates.

    Warnings
    --------
    No dimension checks are implemented but everything needs to have the right
    dimensions. That means `dem.data`, `ifg.data`, `mwr.data` and `swr.data`
    have the same shape. The size of the first two axes of `mmodel`'s and
    `smodel`'s properties needs to be the same as the shape of the
    aforementioned parameters too.

    The latitudes and longitudes of `dem` are used to index all other
    parameters. Everything needs to be on the same grid as a result.
    """
    # Divide input matrices into view windows.
    stride = (int(patch_size[0] / 2), int(patch_size[1] / 2)) # Overlap boundaries
    pdem = view_as_windows(dem.data, patch_size, stride)
    plats = view_as_windows(dem.lats, (patch_size[0],), (stride[0],))
    plons = view_as_windows(dem.lons, (patch_size[1],), (stride[1],))

    pifg = view_as_windows(ifg.data, patch_size, stride)
    pmwr = view_as_windows(mwr.data, patch_size, stride)
    pswr = view_as_windows(swr.data, patch_size, stride)

    nplevels = mmodel.pressure.shape[2]
    pmrel_hum = view_as_windows(mmodel.rel_hum, patch_size + (nplevels,), stride + (nplevels,))
    pmtemp = view_as_windows(mmodel.temp, patch_size + (nplevels,), stride + (nplevels,))
    pmgeopot = view_as_windows(mmodel.geopot, patch_size + (nplevels,), stride + (nplevels,))
    pmpressure = view_as_windows(mmodel.pressure, patch_size + (nplevels,), stride + (nplevels,))

    psrel_hum = view_as_windows(smodel.rel_hum, patch_size + (nplevels,), stride + (nplevels,))
    pstemp = view_as_windows(smodel.temp, patch_size + (nplevels,), stride + (nplevels,))
    psgeopot = view_as_windows(smodel.geopot, patch_size + (nplevels,), stride + (nplevels,))
    pspressure = view_as_windows(smodel.pressure, patch_size + (nplevels,), stride + (nplevels,))

    # Preallocate output matrices and divide them into patches
    mwet = np.zeros(dem.data.shape)
    mdry = np.zeros(dem.data.shape)
    swet = np.zeros(dem.data.shape)
    sdry = np.zeros(dem.data.shape)
    m_plevels = np.zeros(dem.data.shape)
    s_plevels = np.zeros(dem.data.shape)

    pmwet = view_as_windows(mwet, patch_size, stride)
    pmdry = view_as_windows(mdry, patch_size, stride)
    pswet = view_as_windows(swet, patch_size, stride)
    psdry = view_as_windows(sdry, patch_size, stride)
    pm_plevels = view_as_windows(m_plevels, patch_size, stride)
    ps_plevels = view_as_windows(s_plevels, patch_size, stride)

    # Iterate through patch by patch
    for y, x in np.ndindex(pdem.shape[0], pdem.shape[1]):
        # Check if there's no rainfall on both dates. If so just do a normal
        # weather model correction.
        logging.info('Processing patch (%d, %d) of (%d, %d) for interferogram '
                     '%s / %s', y, x, pdem.shape[0] - 1, pdem.shape[1] - 1,
                     ifg.master_date, ifg.slave_date)
        if pmwr[y, x, :, :].sum() == 0 and pswr[y, x, :, :].sum() == 0:
            logging.debug('No rainfall in patch (%d, %d)', y, x)
            # Construct new DEM and ERAModel objects from patches
            sub_mmodel = ERAModel(plats[y], plons[x], mmodel.date,
                                  pmrel_hum[y, x, 0],
                                  pmtemp[y, x, 0],
                                  pmgeopot[y, x, 0],
                                  pmpressure[y, x, 0])
            sub_smodel = ERAModel(plats[y], plons[x], mmodel.date,
                                  psrel_hum[y, x, 0],
                                  pstemp[y, x, 0],
                                  psgeopot[y, x, 0],
                                  pspressure[y, x, 0])
            sub_dem = GeoGrid(plons[x], plats[y], pdem[y, x])

            sub_mwet, sub_mdry, _ = calculate_era_zenith_delay(sub_mmodel,
                                                               sub_dem)
            sub_swet, sub_sdry, _ = calculate_era_zenith_delay(sub_smodel,
                                                               sub_dem)

            # Update output matrices
            pmwet[y, x, :, :] = sub_mwet.data
            pmdry[y, x, :, :] = sub_mdry.data
            pswet[y, x, :, :] = sub_swet.data
            psdry[y, x, :, :] = sub_sdry.data
            pm_plevels[y, x, :, :] = np.nan
            ps_plevels[y, x, :, :] = np.nan

            continue
        else:
            # Time to go with the more complicated algorithm
            logging.debug('Rainfall found in patch (%d, %d)', y, x)
            output = _optimise_zenith_delay(pdem[y, x],
                                            pifg[y, x],
                                            pmwr[y, x],
                                            pswr[y, x],
                                            pmtemp[y, x, 0],
                                            pmrel_hum[y, x, 0],
                                            pmpressure[y, x, 0],
                                            pmgeopot[y, x, 0],
                                            pstemp[y, x, 0],
                                            psrel_hum[y, x, 0],
                                            pspressure[y, x, 0],
                                            psgeopot[y, x, 0],
                                            look_angle,
                                            min_plevel)
            pmwet[y, x, :, :] = output[0]
            pmdry[y, x, :, :] = output[1]
            pswet[y, x, :, :] = output[2]
            psdry[y, x, :, :] = output[3]
            pm_plevels[y, x, :, :] = output[4]
            ps_plevels[y, x, :, :] = output[5]

    return (SAR(dem.lons, dem.lats, mwet, ifg.master_date),
            SAR(dem.lons, dem.lats, mdry, ifg.master_date),
            SAR(dem.lons, dem.lats, swet, ifg.slave_date),
            SAR(dem.lons, dem.lats, sdry, ifg.slave_date),
            m_plevels, s_plevels)


# UGLY FUNCTION SIGNATURE.
def _optimise_zenith_delay(pdem, pifg, pmwr, pswr, pmtemp, pmrel_hum,
                           pmpressure, pmgeopot, pstemp, psrel_hum, pspressure,
                           psgeopot, look_angle, min_plevel):
    max_plevel_idx = 0
    try:
        min_plevel_idx = np.nonzero(pmpressure[0, 0, :] == min_plevel)[0][0]
    except IndexError:
        raise IndexError('Min pressure level could not be found in the '
                         'weather model')

    # Create output patches
    pmwet = np.zeros(pdem.shape)
    pmdry = np.zeros(pdem.shape)
    pswet = np.zeros(pdem.shape)
    psdry = np.zeros(pdem.shape)
    pm_plevels = np.empty(pdem.shape)  # 1 HPa initial
    pm_plevels[:, :] = np.nan
    ps_plevels = np.empty(pdem.shape)
    ps_plevels[:, :] = np.nan

    # Dummy lats and lons for constructing objects.
    dummy_lats = np.linspace(0, 1, pdem.shape[0])
    dummy_lons = np.linspace(0, 1, pdem.shape[1])

    pdem_obj = GeoGrid(dummy_lons, dummy_lats, pdem)
    pmmodel_obj = ERAModel(dummy_lats, dummy_lons, None, pmrel_hum, pmtemp,
                           pmgeopot, pmpressure)
    psmodel_obj = ERAModel(dummy_lats, dummy_lons, None, psrel_hum, pstemp,
                           psgeopot, pspressure)

    # Start with the standard deviation of a pure ERA correction.
    logging.debug('Calculating initial standard deviation')
    mwet_init, mdry_init, _ = calculate_era_zenith_delay(pmmodel_obj, pdem_obj)
    swet_init, sdry_init, _ = calculate_era_zenith_delay(psmodel_obj, pdem_obj)
    correction_init = pifg - ((mwet_init.data + mdry_init.data)
                              - (swet_init.data + sdry_init.data))
    # max_std = correction_init.std()
    max_std = np.inf
    pmwet = mwet_init.data
    pmdry = mdry_init.data
    pswet = swet_init.data
    psdry = sdry_init.data
    logging.debug('Initial standard deviation: %.5f', max_std)

    # Avoid doing iteration over pressure levels for patches where there's no
    # rain
    no_master_rain = pmwr.sum() == 0
    no_slave_rain = pswr.sum() == 0

    master_max_plevel_idx = max_plevel_idx
    master_min_plevel_idx = min_plevel_idx
    slave_max_plevel_idx = max_plevel_idx
    slave_min_plevel_idx = min_plevel_idx

    if no_master_rain:
        logging.debug('Master radar patch contains no rain')
        master_max_plevel_idx = 0
        master_min_plevel_idx = 0

    if no_slave_rain:
        logging.debug('Slave radar patch contains no rain')
        slave_max_plevel_idx = 0
        slave_min_plevel_idx = 0

    for pm_idx in range(master_max_plevel_idx, master_min_plevel_idx + 1):
        for ps_idx in range(slave_max_plevel_idx, slave_min_plevel_idx + 1):
            logging.debug('Testing master pressure level %d slave pressure '
                          'level %d', pmpressure[0, 0, pm_idx],
                          pspressure[0, 0, ps_idx])
            # Reset relative humidity in master and slave models to their
            # original values.
            pmmodel_obj.rel_hum = pmrel_hum.copy()
            psmodel_obj.rel_hum = psrel_hum.copy()

            # Add rainfall
            if not no_master_rain:
                pmmodel_obj.add_rainfall(pmwr, pmpressure[0, 0, pm_idx], 1000, 10)
            if not no_slave_rain:
                psmodel_obj.add_rainfall(pswr, pspressure[0, 0, ps_idx], 1000, 10)

            # Calculate the zenith delays
            mwet_zen, mdry_zen, _ = calculate_era_zenith_delay(pmmodel_obj,
                                                               pdem_obj)
            swet_zen, sdry_zen, _ = calculate_era_zenith_delay(psmodel_obj,
                                                               pdem_obj)

            # Convert to slants
            mwet_slant = mwet_zen.zenith2slant(look_angle)
            mdry_slant = mdry_zen.zenith2slant(look_angle)
            swet_slant = swet_zen.zenith2slant(look_angle)
            sdry_slant = sdry_zen.zenith2slant(look_angle)

            # Compute total delay and then interferometric delay
            mtotal = mwet_slant.data + mdry_slant.data
            stotal = swet_slant.data + sdry_slant.data
            ifg_total = mtotal - stotal

            # Apply correction and check standard deviation. If it's improved
            # store the updated models, otherwise we move on.
            corrected_ifg = pifg - ifg_total

            if corrected_ifg.std() < max_std:
                pmwet[:, :] = mwet_zen.data.copy()
                pmdry[:, :] = mdry_zen.data.copy()
                pswet[:, :] = swet_zen.data.copy()
                psdry[:, :] = sdry_zen.data.copy()
                pm_plevels[:, :] = np.nan if no_master_rain else pmpressure[0, 0, pm_idx]
                ps_plevels[:, :] = np.nan if no_slave_rain else pspressure[0, 0, ps_idx]
                max_std = corrected_ifg.std()
                logging.debug('Accepting model with standard deviation %.5f',
                              max_std)
            else:
                logging.debug('Rejecting model with standard deviation %.5f',
                              corrected_ifg.std())

    return (pmwet, pmdry, pswet, psdry, pm_plevels, ps_plevels)


@jit
def calculate_era_zenith_delay(model, dem):
    """Calculates the one-way zenith delay in cm predicted by a weather
    model.

    Arguments
    --------
    model : ERAModel
      The weather model to use in the delay calculation.
    dem : geogrid.GeoGrid
      A GeoGrid instance defining the DEM in metres.

    Returns
    -------
    A three tuple containing (wet_delay, dry_delay and total_delay) that are
    instances of insar.SAR containing the one-way zenith delay in centimetres.

    Notes
    -----
    `model` should be interpolated so it is on the same grid as `dem`.
    """
    if (dem.lats.size != model.lats.size
        or dem.lons.size != model.lons.size):
        raise IndexError('Size of model grid does not match size of dem')

    # Allocate output arrays
    wet_delay = np.empty((model.lats.size, model.lons.size))
    dry_delay = np.empty((model.lats.size, model.lons.size))

    # Cache the height and ppwv to avoid recalculating at every pixel.
    height = model.height
    ppwv = model.ppwv

    dry_delay = np.empty(dem.data.shape)
    wet_delay = np.empty(dem.data.shape)

    _calculate_zenith_delay_jit(dem.data, height, model.temp, ppwv,
                                model.pressure, wet_delay, dry_delay)

    wet_delay = SAR(model.lons, model.lats, wet_delay, model.date)
    dry_delay = SAR(model.lons, model.lats, dry_delay, model.date)
    total_delay = SAR(model.lons, model.lats, dry_delay.data + wet_delay.data,
                      model.date)

    return (wet_delay, dry_delay, total_delay)


@jit(numba.void(numba.float64[:, :],
                numba.float64[:, :, :],
                numba.float64[:, :, :],
                numba.float64[:, :, :],
                numba.float64[:, :, :],
                numba.float64[:, :],
                numba.float64[:, :]),
     nopython=True)
def _calculate_zenith_delay_jit(dem, height, temp, ppwv, pressure, out_wet, out_dry):
    """JIT optimised function to calculate the zenith wet and dry delay.

    Arguments
    ---------
    dem : (n,m) ndarray
      DEM in matrix form.
    height : (n,m,o) ndarray
      Heights of the weather model.
    temp : (n,m,o) ndarray
      Weather model temperatures
    ppwv : (n,m,o) ndarray
      Weather model partial pressure of water vapour
    pressure : (n,m,o) ndarray
      Weather model pressure.
    out_wet : (n,m) ndarray
      Matrix to save wet delay into.
    out_dry : (n,m) ndarray
      Matrix to save dry delay into.

    Returns
    -------
    None

    """
    # The implementation here is loosely based off of the implementation in
    # TRAIN by David Bekaert. There are some differences that give slightly
    # different numerical results. Notably, the dry delay is found by
    # integrating pressure with height rather than by calculating the surface
    # pressure. Note the performance is rather poor due to the reliance on a
    # loop. This can probably be made faster.
    # Constants and formulae from Hanssen 2001
    k1 = 0.776  # K Pa^{-1}
    k2 = 0.716  # K Pa^{-1}
    k3 = 3.75 * 10**3  # K^2 Pa{-1}
    Rd = 287.053
    Rv = 461.524

    nheights = 1024

    # Caches for the interpolated variables. Needed for JIT evaluation of the
    # interpolation function.
    itemp = np.zeros(nheights)
    ippwv = np.zeros(nheights)
    ipressure = np.zeros(nheights)
    for (y, x), _ in np.ndenumerate(dem):
        new_heights = np.linspace(dem[y, x], 15000, nheights)
        interp1d_jit(height[y, x, :], temp[y, x, :], new_heights, itemp)
        interp1d_jit(height[y, x, :], ppwv[y, x, :], new_heights, ippwv)
        interp1d_jit(height[y, x, :], pressure[y, x, :], new_heights, ipressure)

        # Convert pressures from hPa to Pa
        ipressure *= 100
        ippwv *= 100

        wet_refract = ((k2 - (k1 * Rd / Rv)) * (ippwv / itemp)
                       + (k3 * ippwv / (itemp**2)))
        dry_refract = k1 * ipressure / itemp

        # Convert delays to centimetres
        out_wet[y, x] = 10**-6 * trapz(new_heights, wet_refract) * 100
        out_dry[y, x] = 10**-6 * trapz(new_heights, dry_refract) * 100


def liquid_zenith_delay(lwc, cloud_thickness):
    """Calculate the liquid delay assuming a constant liquid water content.

    Arguments
    ---------
    lwc : nimrod.Nimrod
      Nimrod instance containing the liquid water content in g/m^3.
    cloud_thickness (n,m) ndarry OR float
      A matrix containing the cloud thickness at each grid point. Pass a single
      float for a constant cloud thickness.

    Returns
    -------
    (n,m) ndarray containing the liquid zenith delay in centimetres.
    """
    delay = 0.145 * lwc.data * cloud_thickness
    return SAR(lwc.lons, lwc.lats, delay, lwc.date)


def calc_ifg_delay(master_delay, slave_delay):
    """Calculate the interferometric delay.

    Arguments
    ---------
    master_delay : (n,m) ndarray
      Matrix containing the atmospheric delay on the master date.
    slave_delay : (n,m) ndarray
      Matrix containing the atmospheric delay on the slave date.

    Returns
    -------
    A matrix of size (n,m) containing the interferometric delay.
    """
    return master_delay - slave_delay
