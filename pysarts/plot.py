"""Module for interactive plotting of interferograms.

"""
import os
from datetime import date, datetime
import logging
from multiprocessing.pool import Pool
from scipy.ndimage import gaussian_filter

import matplotlib.cm as cm
import matplotlib.dates as mpl_dates
import matplotlib.pyplot as plt
from mpl_toolkits.basemap import Basemap
import numpy as np
import yaml

from . import inversion
from . import insar
from . import workflow
from . import nimrod
from . import util
from . import config
from . import train
import argparse

plt.style.use('ggplot')
params = {
    'axes.labelsize': 9,
    'font.size': 9,
    'xtick.labelsize': 8,
    'ytick.labelsize': 8,
    'font.family': 'Source Sans Pro',
}

plt.rcParams.update(params)

COAST_DETAIL = 'f'
FIGSIZE = None
DPI = None


def _parse_unwrapped_ifg_args(args):
    if args.time_series:
        plot_time_series_ifg(args.master, args.slave, args.output)
    else:
        kind = 'original'
        if args.resampled:
            kind = 'resampled'
        if args.corrected:
            kind = 'corrected'

        plot_unwrapped_ifg(args.master, args.slave, args.output, kind)


def plot_unwrapped_ifg(master_date, slave_date, fname=None, kind='original'):
    """
    master_date, slc_date : str or date
      If a string, should be in the format '%Y%m%d'.
    fname : str, opt
      The path to save the image to or `None`. If set, interactive plotting will
      be disabled.
    kind : str, opt
      One of 'original', 'resampled', 'corrected' meaning to plot the original,
      resampled or corrected interferogram.

    Returns
    -------
    None
    """
    if isinstance(master_date, date):
        master_date = master_date.strftime("%Y%m%d")
    if isinstance(slave_date, date):
        slave_date = slave_date.strftime("%Y%m%d")

    logging.info('Plotting master/slave pairing: {} / {}'.format(master_date,
                                                                 slave_date))
    ifg_name = '{}_{}'.format(slave_date, master_date)
    ifg_path = ''
    ifg = None
    if kind == 'resampled':
        ifg_path = os.path.join(config.SCRATCH_DIR, 'uifg_resampled', ifg_name + '.npy')
        data = np.load(ifg_path)
        lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                               'grid.txt'))

        ifg = insar.InSAR(lons,
                          lats,
                          data,
                          datetime.strptime(master_date, '%Y%m%d').date(),
                          datetime.strptime(slave_date, '%Y%m%d').date())
    elif kind == 'original':
        ifg_path = os.path.join(config.UIFG_DIR, ifg_name + '.nc')
        ifg = insar.InSAR.from_netcdf(ifg_path)
    elif kind == 'corrected':
        ifg_path = os.path.join(config.SCRATCH_DIR, 'corrected_ifg', ifg_name + '.nc')
        ifg = insar.InSAR.from_netcdf(ifg_path)
    else:
        raise ValueError('Unknown plotting mode {}'.format(kind))

    fig, _ = plot_ifg(ifg)
    if fname:
        fig.savefig(fname, bbox_inches='tight')
        plt.close()
    else:
        plt.show()
        plt.close()

    return None


def plot_ifg(ifg, axes=None, center_zero=True):
    """Plot an insar.InSAR instance or an insar.SAR instance. Returns a figure
    handle"""

    if axes:
        fig = axes.get_figure()
    else:
        fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
        axes = fig.add_subplot(1, 1, 1)

    bmap = Basemap(llcrnrlon=ifg.lons[0],
                   llcrnrlat=ifg.lats[0],
                   urcrnrlon=ifg.lons[-1],
                   urcrnrlat=ifg.lats[-1],
                   resolution=COAST_DETAIL,
                   projection='merc',
                   ax=axes)
    parallels = np.linspace(ifg.lats[0], ifg.lats[-1], 5)
    meridians = np.linspace(ifg.lons[0], ifg.lons[-1], 5)

    bmap.drawcoastlines()
    bmap.drawparallels(parallels, labels=[True, False, False, False],
                       fmt="%.2f", fontsize=plt.rcParams['ytick.labelsize'])
    bmap.drawmeridians(meridians, labels=[False, False, False, True],
                       fmt="%.2f", fontsize=plt.rcParams['xtick.labelsize'])
    bmap.drawmapboundary()

    vmax = (np.absolute(ifg.data).max())
    vmin = vmax * -1

    lon_mesh, lat_mesh = np.meshgrid(ifg.lons, ifg.lats)

    image = None
    if center_zero is True:
        image = bmap.pcolormesh(lon_mesh,
                                lat_mesh,
                                ifg.data,
                                latlon=True,
                                cmap=cm.RdBu_r,
                                vmin=vmin,
                                vmax=vmax)
    else:
        image = bmap.pcolormesh(lon_mesh,
                                lat_mesh,
                                ifg.data,
                                latlon=True,
                                cmap=cm.RdBu_r,)

    cbar = fig.colorbar(image, pad=0.07, ax=axes)
    cbar.set_label('LOS Delay / cm')

    if isinstance(ifg, insar.InSAR):
        title = 'Unwrapped Interferogram\nMaster: {0}\nSlave: {1}'.format(
            ifg.master_date.strftime('%Y-%m-%d'),
            ifg.slave_date.strftime('%Y-%m-%d'))
        axes.set_title(title)
    else:
        title = 'SAR Image ({})'.format(ifg.date.strftime('%Y-%m-%d'))
        axes.set_title(title)

    fig.tight_layout()

    return fig, bmap


def plot_time_series_ifg(master_date, slave_date, fname=None):
    if isinstance(master_date, date):
        master_date = master_date.strftime('%Y%m%d')

    if isinstance(slave_date, date):
        slave_date = slave_date.strftime('%Y%m%d')

    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))
    ifg_ts = np.load(os.path.join(config.SCRATCH_DIR,
                                  'uifg_ts',
                                  master_date + '.npy'),
                     mmap_mode='r')
    slave_date_idx = 0
    with open(os.path.join(config.SCRATCH_DIR,
                           'uifg_ts',
                           master_date + '.yml')) as f:
        ts_date_indexes = yaml.safe_load(f)
        slave_date_date = datetime.strptime(slave_date, '%Y%m%d').date()
        slave_date_idx = ts_date_indexes.index(slave_date_date)

    ifg = insar.InSAR(lons,
                      lats,
                      ifg_ts[:, :, slave_date_idx],
                      datetime.strptime(master_date, '%Y%m%d').date(),
                      datetime.strptime(slave_date, '%Y%m%d').date())

    fig, _ = plot_ifg(ifg)
    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()
        plt.close()


def _plot_dem_error(args):
    if args.master_date:
        plot_dem_error(args.master_date, args.output)
    else:
        plot_dem_error(config.MASTER_DATE.date(), args.output)


def plot_dem_error(master_date, fname=None):
    if isinstance(master_date, date):
        master_date = master_date.strftime('%Y%m%d')

    dem_error = np.load(os.path.join(config.SCRATCH_DIR,
                                     'dem_error',
                                     master_date + '.npy'),
                        mmap_mode='r')
    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))

    sar = insar.SAR(lons,
                    lats,
                    dem_error,
                    datetime.strptime(master_date, '%Y%m%d').date())

    fig, _ = plot_ifg(sar)
    axes = fig.get_axes()[0]
    axes.set_title('DEM Error\n{}'
                   .format(sar.date.strftime('%Y-%m-%d')))
    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()


def _plot_master_atmosphere(args):
    """Plot the master atmosphere from the command line."""
    if args.master_date:
        plot_master_atmosphere(args.master_date, args.output)
    else:
        plot_master_atmosphere(config.MASTER_DATE.date(), args.output)


def plot_master_atmosphere(master_date, fname=None):
    """Plot the master atmosphere for a given date."""
    if isinstance(master_date, date):
        master_date = master_date.strftime('%Y%m%d')

    # Load the master atmosphere
    master_atmosphere = np.load(os.path.join(config.SCRATCH_DIR,
                                             'master_atmosphere',
                                             master_date + '.npy'),
                                mmap_mode='r')
    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))

    sar = insar.SAR(lons,
                    lats,
                    master_atmosphere,
                    datetime.strptime(master_date, '%Y%m%d').date())

    fig, _ = plot_ifg(sar)
    axes = fig.get_axes()[0]
    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        axes.set_title('Master Atmosphere\n{}'
                       .format(sar.date.strftime('%Y-%m-%d')))
        plt.show()


def _plot_delay_rainfall_scatter(args):
    date = args.date or config.MASTER_DATE
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y%m%d')
        date = datetime(date.year, date.month, date.day,
                        config.MASTER_DATE.hour, config.MASTER_DATE.minute)

    plot_delay_rainfall_scatter(date, args.output)


def plot_delay_rainfall_scatter(date, fname=None):
    atmos_path = os.path.join(config.SCRATCH_DIR,
                              'master_atmosphere',
                              date.strftime('%Y%m%d') + '.npy')
    _, wr_path = workflow.find_closest_weather_radar_files(date)

    atmos = np.load(atmos_path)
    wr = nimrod.Nimrod.from_netcdf(wr_path)

    # Load in the grid and resample the weather radar image to it.
    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))
    lon_min, lon_max = lons.min(), lons.max()
    lat_min, lat_max = lats.min(), lats.max()
    lon_bounds = (lon_min - 0.5, lon_max + 0.5)
    lat_bounds = (lat_min - 0.5, lat_max + 0.5)

    wr.clip(lon_bounds, lat_bounds)
    wr.interp(lons, lats, method='nearest')

    fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
    axes = fig.add_subplot(1, 1, 1)
    axes.plot(wr.data.ravel(), atmos.ravel(), 'o', markersize=1)

    axes.set_xlabel(r'Rainfall Rate / mm hr$^{-1}$')
    axes.set_ylabel(r'LOS Delay / cm')
    axes.set_title('Rainfall Scatter ({})'
                   .format(date.strftime('%Y-%m-%d')))

    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def plot_wr(wr, axes=None):
    """Plot a `nimrod.Nimrod` instance. Returns a figure handle and a basemap
    object."""
    if axes:
        fig = axes.get_figure()
    else:
        fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
        axes = fig.add_subplot(1, 1, 1)

    bmap = Basemap(llcrnrlon=wr.lons[0],
                   llcrnrlat=wr.lats[0],
                   urcrnrlon=wr.lons[-1],
                   urcrnrlat=wr.lats[-1],
                   resolution=COAST_DETAIL,
                   projection='merc',
                   ax=axes)

    parallels = np.linspace(wr.lats[0], wr.lats[-1], 5)
    meridians = np.linspace(wr.lons[0], wr.lons[-1], 5)

    bmap.drawcoastlines()
    bmap.drawparallels(parallels, labels=[True, False, False, False],
                       fmt="%.2f", fontsize=plt.rcParams['ytick.labelsize'])
    bmap.drawmeridians(meridians, labels=[False, False, False, True],
                       fmt="%.2f", fontsize=plt.rcParams['xtick.labelsize'])
    bmap.drawmapboundary()

    lon_mesh, lat_mesh = np.meshgrid(wr.lons, wr.lats)
    image = bmap.pcolormesh(lon_mesh,
                            lat_mesh,
                            np.ma.masked_values(wr.data, 0),
                            latlon=True,
                            cmap=cm.Spectral_r,
                            vmin=0)

    cbar = fig.colorbar(image, pad=0.07, ax=axes)
    cbar.set_label(r'Rainfall / mm hr$^{-1}$')

    if wr.interpolated:
        title = ('Rainfall Radar Image\n({0})[I]'
                 .format(wr.date.strftime('%Y-%m-%dT%H:%M')))
    else:
        title = ('Rainfall Radar Image\n({0})'
                 .format(wr.date.strftime('%Y-%m-%dT%H:%M')))

    axes.set_title(title)
    fig.tight_layout()

    return (fig, bmap)


def _plot_weather(args):
    if args.date:
        plot_weather(args.date, args.full, args.output)
    else:
        plot_weather(config.MASTER_DATE, args.full, args.output)


def plot_weather(wr_date, full=False, fname=None):
    """Plot a weather radar image.
    """
    if isinstance(wr_date, str):
        wr_date = datetime.strptime(wr_date, '%Y%m%dT%H%M')

    # Load the weather radar image
    wr_before, wr_after = workflow.find_closest_weather_radar_files(wr_date)

    if wr_before != wr_after:
        logging.warning('Found two different radar images near %s. Interpolating',
                        wr_date)

    wr_before = nimrod.Nimrod.from_netcdf(wr_before)
    wr_after = nimrod.Nimrod.from_netcdf(wr_after)
    # wr = nimrod.Nimrod.interp_radar(wr_before, wr_after, wr_date)
    wr = wr_after

    if not full:
        # Clip image to target region
        lon_bounds = (config.REGION['lon_min'], config.REGION['lon_max'])
        lat_bounds = (config.REGION['lat_min'], config.REGION['lat_max'])
        wr.clip(lon_bounds, lat_bounds)

    fig, _ = plot_wr(wr)

    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()
        plt.close()


def _plot_profile(args):
    date = args.date or config.MASTER_DATE
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y%m%d')
        date = datetime(date.year, date.month, date.day,
                        config.MASTER_DATE.hour, config.MASTER_DATE.minute)

    plot_profile(date, args.longitude, args.blur, args.output)


def plot_profile(master_date, longitude, filter_std, fname=None):
    if isinstance(master_date, str):
        master_date = datetime.strptime(master_date, '%Y%m%dT%H%M')

    fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
    sar_ax = plt.subplot2grid((2, 2), (0, 0))
    wr_ax = plt.subplot2grid((2, 2), (0, 1))
    profile_ax = plt.subplot2grid((2, 2), (1, 0), colspan=2)

    # Load master atmosphere for master date.
    master_atmosphere = np.load(os.path.join(config.SCRATCH_DIR,
                                             'master_atmosphere',
                                             master_date.strftime('%Y%m%d') + '.npy'),
                                mmap_mode='r')
    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))

    sar = insar.SAR(lons,
                    lats,
                    master_atmosphere,
                    master_date)

    # Load weather radar image, clip and resample to IFG resolution.
    lon_bounds = (np.amin(sar.lons), np.amax(sar.lons))
    lat_bounds = (np.amin(sar.lats), np.amax(sar.lats))

    _, wr_after_path = workflow.find_closest_weather_radar_files(master_date)
    wr = nimrod.Nimrod.from_netcdf(wr_after_path)

    wr.clip(lon_bounds, lat_bounds)
    wr.interp(sar.lons, sar.lats, method='nearest')
    wr.data = gaussian_filter(wr.data, filter_std)

    # Plot and configure sar
    _, bmap_sar = plot_ifg(sar, axes=sar_ax)
    sar_ax.set_title('Master Atmosphere\n({})'.format(master_date.strftime('%Y-%m-%dT%H:%M')))
    bmap_sar.plot([longitude, longitude], lat_bounds, latlon=True, linewidth=1, color='white', ax=sar_ax)
    bmap_sar.plot([longitude, longitude], lat_bounds, latlon=True, linewidth=0.5, ax=sar_ax)

    # Plot and configure weather radar image
    _, bmap_wr = plot_wr(wr, axes=wr_ax)
    bmap_wr.plot([longitude, longitude], lat_bounds, latlon=True, linewidth=1, color='white', ax=wr_ax)
    bmap_wr.plot([longitude, longitude], lat_bounds, latlon=True, linewidth=0.5, ax=wr_ax)

    # Plot the profile
    ## LOS Delay
    sar_lon_idx = np.argmin(np.absolute(sar.lons - longitude))
    wr_lon_idx = np.argmin(np.absolute(wr.lons - longitude))
    profile_ax.plot(sar.lats, sar.data[:, sar_lon_idx], color='#67a9cf')
    profile_ax.tick_params('y', colors='#67a9cf')
    profile_ax.set_ylabel('LOS Delay / cm')
    profile_ax.set_xlabel(r'Latitude / $\degree$')
    profile_ax.grid(b=False, axis='y')

    ## Rainfall
    profile_ax_rain = profile_ax.twinx()
    profile_ax_rain.plot(wr.lats, wr.data[:, wr_lon_idx], color='#ef8a62')
    profile_ax_rain.tick_params('y', colors='#ef8a62')
    profile_ax_rain.set_ylabel(r'Rainfall / mm hr$^{-1}$')
    profile_ax_rain.grid(b=False)

    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()
        plt.close()


def _plot_baseline_plot(args):
    if args.master_date:
        plot_baseline_plot(args.master_date, args.output)
    else:
        plot_baseline_plot(config.MASTER_DATE, args.output)


def plot_baseline_plot(master_date, fname=None):
    """Make a baseline plot using the baselines in config.BPERP_FILE_PATH"""
    if isinstance(master_date, str):
        master_date = datetime.strptime(master_date, '%Y%m%d').date()
    if isinstance(master_date, datetime):
        master_date = master_date.date()

    baseline_list = inversion.calculate_inverse_bperp(config.BPERP_FILE_PATH,
                                                      master_date)
    baseline_list = list(baseline_list)
    slave_dates = [date for (date, _) in baseline_list]
    baselines = [baseline for (_, baseline) in baseline_list]
    bperp_contents = inversion.read_bperp_file(config.BPERP_FILE_PATH)
    ifg_master_dates = [date for (date, _, _) in bperp_contents]
    ifg_slave_dates = [date for (_, date, _) in bperp_contents]
    slc_dates = sorted(set(ifg_master_dates + ifg_slave_dates))

    # Set up the plot
    fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
    axes = fig.add_subplot(1, 1, 1)

    # Plot lines connecting dates for interferograms
    line_color = axes._get_lines.get_next_color()
    line = None
    for (master, slave) in zip(ifg_master_dates, ifg_slave_dates):
        master_perp_base = baselines[slave_dates.index(master)]
        slave_perp_base = baselines[slave_dates.index(slave)]

        line = axes.plot_date([master, slave],
                              [master_perp_base, slave_perp_base],
                              '-',
                              linewidth=0.5,
                              color=line_color,
                              label='Interferogram Pairing')

    # Plot Acquisitions
    xs = []  # Time baseline in days
    ys = []  # Perpendicular baseline in metres
    for slc_date in slc_dates:
        xs += [slc_date]
        ys += [baselines[slave_dates.index(slc_date)]]

    points = axes.plot_date(xs, ys, label='Acquisition')

    # Axes styling
    axes.legend(handles=[points[0], line[0]])
    axes.set_xlabel('Date')
    axes.set_ylabel('Perpendicular Baseline / m')
    axes.set_title('Baseline Plot')
    axes.xaxis.set_major_formatter(mpl_dates.DateFormatter('%Y-%b'))

    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def _plot_train_sar_delay(args):
    if not args.hydrostatic and not args.wet and not args.total:
        # TODO: Print to stderr
        print("Error, must specify one of hydro, wet or total")
        exit(1)

    date = args.date if args.date else config.MASTER_DATE
    if args.hydrostatic:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_hydro' + comps[1]

        plot_train_sar_delay(date, kind='hydro', output=output)

    if args.wet:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_wet' + comps[1]

        plot_train_sar_delay(date, kind='wet', output=output)

    if args.total:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_total' + comps[1]

        plot_train_sar_delay(date, kind='total', output=output)


def plot_train_sar_delay(master_date, kind='total', output=None):
    """Plot the slant delay for a date computed from ERA by TRAIN

    Arguments
    ---------
    master_date : date
      The date to plot the delay for.
    kind : str, opt
      The type of delay to plot. One of 'hydro', 'wet' or 'total' (default).
    output : str, opt
      Name of the file to save the plot to.
    """
    if isinstance(master_date, str):
        master_date = datetime.strptime(master_date, '%Y%m%d').date()

    master_datestamp = master_date.strftime('%Y%m%d')
    era_dir = workflow.get_train_era_slant_dir()
    delay_fpath = os.path.join(era_dir, master_datestamp + '.mat')

    era_delays = train.load_train_slant_delay(delay_fpath)
    data = np.zeros(era_delays['wet_delay'].shape)
    if kind == 'wet':
        data[:, :] = era_delays['wet_delay']
    elif kind == 'hydro':
        data[:, :] = era_delays['hydro_delay']
    elif kind == 'total':
        data[:, :] = era_delays['wet_delay'] + era_delays['hydro_delay']
    else:
        raise KeyError('Unknown kind {}'.format(kind))

    # Mask the data to remove NaNs
    data = np.ma.masked_invalid(data)

    # Get a mask for water from one of the interferograms
    ifg_path = os.path.join(config.SCRATCH_DIR,
                            'master_atmosphere',
                            config.MASTER_DATE.strftime('%Y%m%d') + '.npy')
    ifg_data = np.load(ifg_path)
    ifg_data = np.ma.masked_values(ifg_data, 0)

    # Combine masks
    data.mask = ifg_data.mask | data.mask

    sar = insar.SAR(era_delays['lons'],
                    era_delays['lats'],
                    data,
                    master_date)

    fig, bmap = plot_ifg(sar, center_zero=False)

    title_map = {'total': 'Total', 'hydro': 'Hydrostatic', 'wet': 'Wet'}
    title_str = "{kind:s} Delay\n{date:}".format(kind=title_map[kind],
                                                 date=master_date)

    axes = fig.get_axes()[0]
    axes.set_title(title_str)

    if output:
        fig.savefig(output, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def _plot_train_ifg_delay(args):
    if not args.hydrostatic and not args.wet and not args.total:
        # TODO: Print to stderr
        print("Error, must specify one or more of hydro, wet or total")
        exit(1)

    master_date = args.master_date
    slave_date = args.slave_date
    if args.hydrostatic:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_hydro' + comps[1]

        plot_train_ifg_delay(master_date, slave_date, kind='hydro',
                             output=output)

    if args.wet:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_wet' + comps[1]

        plot_train_ifg_delay(master_date, slave_date, kind='wet',
                             output=output)

    if args.total:
        output = args.output
        if output:
            comps = os.path.splitext(output)
            output = comps[0] + '_total' + comps[1]

        plot_train_ifg_delay(master_date, slave_date, kind='total',
                             output=output)

def plot_train_ifg_delay(master_date, slave_date, kind='total', output=None):
    """Plot the interferometric delay for a date computed from ERA by TRAIN

    Arguments
    ---------
    master_date : date
      The date to plot the delay for.
    slave_date : date
      The slave date to plot the delay for.
    kind : str, opt
      The type of delay to plot. One of 'hydro', 'wet' or 'total' (default).
    output : str, opt
      Name of the file to save the plot to.
    """
    if isinstance(master_date, str):
        master_date = datetime.strptime(master_date, '%Y%m%d').date()
    if isinstance(slave_date, str):
        slave_date = datetime.strptime(slave_date, '%Y%m%d').date()

    train_dir = os.path.join(config.SCRATCH_DIR, 'train')
    correction_fpath = os.path.join(train_dir, 'tca2.mat')
    dates_fpath = os.path.join(train_dir, 'ifgday.mat')
    grid_fpath = os.path.join(train_dir, 'll.mat')

    corrections = train.load_train_ifg_delay(correction_fpath,
                                             grid_fpath,
                                             dates_fpath,
                                             master_date,
                                             slave_date)

    data = np.zeros(corrections['wet_delay'].shape)
    if kind == 'hydro':
        data[:, :] = corrections['hydro_delay']
    elif kind == 'wet':
        data[:, :] = corrections['wet_delay']
    elif kind == 'total':
        data[:, :] = corrections['total_delay']
    else:
        raise KeyError('"kind" was not one of hydro, wet or total')

    # Mask invalid data
    data = np.ma.masked_invalid(data)

    # Get a mask for water from the interferogram
    ifg_path = os.path.join(config.SCRATCH_DIR,
                            'uifg_resampled',
                            (slave_date.strftime('%Y%m%d') + '_' +
                             master_date.strftime('%Y%m%d') + '.npy'))
    ifg_data = np.load(ifg_path)
    ifg_data = np.ma.masked_values(ifg_data, 0)

    # Combine masks
    data.mask = ifg_data.mask | data.mask

    ifg = insar.InSAR(corrections['lons'],
                      corrections['lats'],
                      data,
                      master_date,
                      slave_date)

    fig, bmap = plot_ifg(ifg)
    axes = fig.get_axes()[0]
    title_map = {'wet': 'Wet', 'hydro': 'Hydrostatic', 'total': 'Total'}
    title = '{:s} Delay\nMaster: {:s}\nSlave: {:s}'.format(title_map[kind],
                                                           master_date.strftime('%Y-%m-%d'),
                                                           slave_date.strftime('%Y-%m-%d'))
    axes.set_title(title)

    if output:
        fig.savefig(output, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def _plot_sar_delay(args):
    date = args.date if args.date else config.MASTER_DATE
    kinds = []
    if args.hydrostatic:
        kinds += ['dry']
    if args.wet:
        kinds += ['wet']
    if args.liquid:
        kinds += ['liquid']
    if args.total:
        kinds += ['total']

    for kind in kinds:
        plot_sar_delay(date, kind, args.zenith, args.output)


def plot_sar_delay(date, kind='total', zenith=False, output=None):
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y%m%d').date()

    if kind not in ('total', 'dry', 'wet', 'liquid'):
        raise KeyError('Unknown kind {}'.format(kind))

    datestamp = date.strftime('%Y%m%d')
    delay_dir = ""
    if zenith:
        delay_dir = os.path.join(config.SCRATCH_DIR, 'zenith_delays')
    else:
        delay_dir = os.path.join(config.SCRATCH_DIR, 'slant_delays')

    delay_file = os.path.join(delay_dir, datestamp + '_' + kind + '.npy')
    delay = np.load(delay_file)
    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))

    # Get a mask for water from one of the interferograms
    ifg_path = os.path.join(config.SCRATCH_DIR, 'master_atmosphere',
                            config.MASTER_DATE.strftime('%Y%m%d') + '.npy')
    ifg_data = np.load(ifg_path)
    ifg_data = np.ma.masked_values(ifg_data, 0)

    delay = np.ma.masked_invalid(delay)

    # Combine masks
    delay.mask = ifg_data.mask | delay.mask

    sar = insar.SAR(lons, lats, delay, date)

    fig, bmap = plot_ifg(sar, center_zero=False)

    title_map = {'total': 'Total', 'dry': 'Hydrostatic', 'wet': 'Wet',
                 'liquid': 'Liquid'}
    title_str = "{kind:s} Delay\n{date:}".format(kind=title_map[kind],
                                                 date=date)

    if zenith:
        title_str += ' (Zenith)'

    axes = fig.get_axes()[0]
    axes.set_title(title_str)

    if output:
        fig.savefig(output, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def _plot_insar_delay(args):
    plot_insar_delay(args.master_date, args.slave_date, args.output)


def plot_insar_delay(master_date, slave_date, output=None):
    if isinstance(master_date, str):
        master_date = datetime.strptime(master_date, '%Y%m%d')
    if isinstance(slave_date, str):
        slave_date = datetime.strptime(slave_date, '%Y%m%d')

    delay_dir = os.path.join(config.SCRATCH_DIR, 'insar_atmos_delays')
    delay_fname = (slave_date.strftime('%Y%m%d') + '_'
                   + master_date.strftime('%Y%m%d') + '.npy')
    delay_fpath = os.path.join(delay_dir, delay_fname)

    delay = np.load(delay_fpath)

    lons, lats = workflow.read_grid_from_file(os.path.join(config.SCRATCH_DIR,
                                                           'grid.txt'))

    # Get a mask for water from one of the interferograms
    ifg_path = os.path.join(config.SCRATCH_DIR,
                            'uifg_resampled',
                            (slave_date.strftime('%Y%m%d') + '_'
                             + master_date.strftime('%Y%m%d') + '.npy'))
    ifg_data = np.load(ifg_path)
    ifg_data = np.ma.masked_values(ifg_data, 0)

    delay = np.ma.masked_invalid(delay)

    # Combine masks
    delay.mask = ifg_data.mask | delay.mask

    ifg = insar.InSAR(lons, lats, delay, master_date, slave_date)

    fig, bmap = plot_ifg(ifg)

    title_str = ("Total Delay\nMaster Date: {}\nSlave Date: {}"
                 .format(master_date.strftime('%Y-%m-%d'),
                         slave_date.strftime('%Y-%m-%d')))
    axes = fig.get_axes()[0]
    axes.set_title(title_str)

    if output:
        fig.savefig(output, bbox_inches='tight')
    else:
        plt.show()

    plt.close()


def _plot_lwc(args):
    date = datetime.strptime(args.date, '%Y%m%d').date()

    plot_lwc(date, args.output)


def plot_lwc(date, fname=None):
    lwc_file = os.path.join(config.SCRATCH_DIR, 'lwc',
                            date.strftime('%Y%m%d') + '.npy')
    data = np.load(lwc_file)
    print(data.max())

    grid_file = os.path.join(config.SCRATCH_DIR, 'grid.txt')
    lons, lats = workflow.read_grid_from_file(grid_file)

    fig = plt.figure(dpi=DPI, figsize=FIGSIZE)
    axes = fig.add_subplot(1, 1, 1)

    bmap = Basemap(llcrnrlon=lons[0],
                   llcrnrlat=lats[0],
                   urcrnrlon=lons[-1],
                   urcrnrlat=lats[-1],
                   resolution=COAST_DETAIL,
                   projection='merc',
                   ax=axes)

    if FIGSIZE is not None and FIGSIZE[0] <= 2:
        parallels = np.linspace(lats[0], lats[-1], 2)
        meridians = np.linspace(lons[0], lons[-1], 2)
    else:
        parallels = np.linspace(lats[0], lats[-1], 5)
        meridians = np.linspace(lons[0], lons[-1], 5)

    bmap.drawcoastlines()
    bmap.drawparallels(parallels, labels=[True, False, False, False],
                       fmt="%.2f", fontsize=plt.rcParams['ytick.labelsize'])
    bmap.drawmeridians(meridians, labels=[False, False, False, True],
                       fmt="%.2f", fontsize=plt.rcParams['xtick.labelsize'])
    bmap.drawmapboundary()

    lon_mesh, lat_mesh = np.meshgrid(lons, lats)
    image = bmap.pcolormesh(lon_mesh,
                            lat_mesh,
                            data,
                            latlon=True,
                            cmap=cm.Blues,
                            vmin=0,
                            vmax=data.max())

    cbar = fig.colorbar(image, pad=0.07, ax=axes)
    cbar.set_label(r'Liquid Water Content / g m$^{-3}$')

    fig.tight_layout()
    if fname:
        fig.savefig(fname, bbox_inches='tight')
    else:
        plt.show()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='pysarts.plot')
    parser.add_argument('-d', '--directory', action='store', default='.',
                        help=('The project directory'))
    parser.add_argument('-r', '--coast-detail', action='store',
                        default='i',
                        help='Resolution of coastlines in the plot')
    parser.add_argument('-f', '--figsize', action='store', nargs=2, type=float,
                        default=None,
                        help='The width and height of the figure in inches')
    parser.add_argument('--dpi', action='store', type=int, default=None,
                        help='The dpi of the figure')

    subparsers = parser.add_subparsers()

    # Plot unwrapped interferogram parser
    plot_uifg_subparser = subparsers.add_parser('uifg',
                                                help='Plot an unwrapped interferogram')
    plot_uifg_subparser.set_defaults(func=_parse_unwrapped_ifg_args)
    plot_uifg_subparser.add_argument('-m', '--master', action='store',
                                     help='Master date (YYYYMMDD)', required=True)
    plot_uifg_subparser.add_argument('-s', '--slave', action='store',
                                     help='Slave date (YYYYMMDD)', required=True)
    plot_uifg_subparser.add_argument('-o', '--output', action='store', default=None,
                                     help='Output filename')
    plot_uifg_subparser.add_argument('-r', '--resampled', action='store_true',
                                     help='Plot the resampled interferogram')
    plot_uifg_subparser.add_argument('-t', '--time-series', action='store_true',
                                     help='Plot inverted time series interferogram')
    plot_uifg_subparser.add_argument('-c', '--corrected', action='store_true',
                                     help='Plot corrected interferogram')

    plot_master_atmosphere_subparser = subparsers.add_parser('master-atmos',
                                                             help='Plot master atmosphere for a date')
    plot_master_atmosphere_subparser.set_defaults(func=_plot_master_atmosphere)
    plot_master_atmosphere_subparser.add_argument('master_date', default=None, nargs='?')
    plot_master_atmosphere_subparser.add_argument('-o', '--output', action='store', default=None,
                                                  help='Output file name')

    plot_dem_error_subparser = subparsers.add_parser('dem-error',
                                                     help='Plot DEM error constant for a date')
    plot_dem_error_subparser.set_defaults(func=_plot_dem_error)
    plot_dem_error_subparser.add_argument('master_date', default=None, nargs='?')
    plot_dem_error_subparser.add_argument('-o', '--output', action='store', default=None,
                                          help='Output file name')

    rain_scatter_parser = subparsers.add_parser('rain-scatter',
                                                help=('Plot a rainfall vs. LOS '
                                                      'delay scatter chart'))
    rain_scatter_parser.set_defaults(func=_plot_delay_rainfall_scatter)
    rain_scatter_parser.add_argument('-d', '--date', action='store',
                                     default=None,
                                     help='Date to plot rainfall scatter for')
    rain_scatter_parser.add_argument('-o', '--output', action='store',
                                     default=None, help='Output file name')

    plot_radar_rainfall_subparser = subparsers.add_parser('weather',
                                                          help='Plot a rainfall radar image')
    plot_radar_rainfall_subparser.set_defaults(func=_plot_weather)
    plot_radar_rainfall_subparser.add_argument('date', default=None, nargs='?',
                                               help=('The date and time (HH:MM) to plot'
                                                     + ' the weather for in ISO8601 format'))
    plot_radar_rainfall_subparser.add_argument('-o', '--output',
                                               action='store',
                                               default=None,
                                               help='Output file name')
    plot_radar_rainfall_subparser.add_argument('-f', '--full',
                                               action='store_true',
                                               help='Plot the entire radar image instead of just the project region')

    plot_profile_subparser = subparsers.add_parser('profile',
                                                   help='Plot master atmosphere LOS delay and rainfall along a profile')
    plot_profile_subparser.set_defaults(func=_plot_profile)
    plot_profile_subparser.add_argument('--longitude',
                                        action='store',
                                        default=None,
                                        help='The line of longitude to plot along',
                                        nargs=1,
                                        type=float,
                                        required=True)
    plot_profile_subparser.add_argument('-d', '--date',
                                        action='store',
                                        default=None,
                                        help='The date to plot a profile for')
    plot_profile_subparser.add_argument('-b', '--blur',
                                        action='store',
                                        default=0,
                                        type=float,
                                        help=('Standard deviation of Gaussian '
                                              'filter to apply to rainfall '
                                              'data'))
    plot_profile_subparser.add_argument('-o', '--output',
                                        action='store',
                                        default=None,
                                        help='Output file name')

    baseline_plot_subparser = subparsers.add_parser('baseline',
                                                    help='Make a baseline plot')
    baseline_plot_subparser.set_defaults(func=_plot_baseline_plot)
    baseline_plot_subparser.add_argument('-o', '--output',
                                         action='store',
                                         default=None,
                                         help='Output file name')
    baseline_plot_subparser.add_argument('-m', '--master-date',
                                         action='store',
                                         default=None,
                                         help='Master date of baseline plot')

    train_sar_delay_parser = subparsers.add_parser('era-slant-delay',
                                                   help='Plot slant delay for a single date calculated by ERA')
    train_sar_delay_parser.add_argument('-d', '--date',
                                        action='store',
                                        default=None,
                                        help='Date to plot delay for')
    train_sar_delay_parser.add_argument('-y', '--hydrostatic',
                                        action='store_true',
                                        help='Plot the hydrostatic delay')
    train_sar_delay_parser.add_argument('-w', '--wet',
                                        action='store_true',
                                        help='Plot the wet delay')
    train_sar_delay_parser.add_argument('-t', '--total',
                                        action='store_true',
                                        help='Plot the total delay')
    train_sar_delay_parser.add_argument('-o', '--output',
                                        action='store',
                                        default=None,
                                        help='Output file name')
    train_sar_delay_parser.set_defaults(func=_plot_train_sar_delay)

    train_insar_delay_parser = subparsers.add_parser('train-insar-delay',
                                                     help=('Plot interferometric atmospheric'
                                                           'delays calculated by TRAIN from ERA'))
    train_insar_delay_parser.add_argument('-m', '--master-date',
                                          action='store',
                                          default=None,
                                          required=True,
                                          help='Master date to plot delay for')
    train_insar_delay_parser.add_argument('-s', '--slave-date',
                                          action='store',
                                          default=None,
                                          required=True,
                                          help='Slave date to plot delay for')
    train_insar_delay_parser.add_argument('-y', '--hydrostatic',
                                          action='store_true',
                                          help='Plot the hydrostatic delay')
    train_insar_delay_parser.add_argument('-w', '--wet',
                                          action='store_true',
                                          help='Plot the wet delay')
    train_insar_delay_parser.add_argument('-t', '--total',
                                          action='store_true',
                                          help='Plot the total delay')
    train_insar_delay_parser.add_argument('-o', '--output',
                                          action='store',
                                          default=None,
                                          help='Output file name')
    train_insar_delay_parser.set_defaults(func=_plot_train_ifg_delay)

    sar_delay_parser = subparsers.add_parser('sar-delay',
                                             help=('Plot SAR delays computed by pysarts'))
    sar_delay_parser.add_argument('-d', '--date',
                                  action='store',
                                  default=None,
                                  help='Date to plot delay for')
    sar_delay_parser.add_argument('-z', '--zenith',
                                  action='store_true',
                                  help='Plot zenith delays instead of slant delays')
    sar_delay_parser.add_argument('-y', '--hydrostatic',
                                  action='store_true',
                                  help='Plot the dry delay')
    sar_delay_parser.add_argument('-w', '--wet',
                                  action='store_true',
                                  help='Plot the wet delay')
    sar_delay_parser.add_argument('-l', '--liquid',
                                  action='store_true',
                                  help='Plot the liquid delay')
    sar_delay_parser.add_argument('-t', '--total',
                                  action='store_true',
                                  help='Plot wet + dry [+ liquid] delay')
    sar_delay_parser.add_argument('-o', '--output',
                                  action='store',
                                  default=None,
                                  help='Output file name')
    sar_delay_parser.set_defaults(func=_plot_sar_delay)

    insar_delay_parser = subparsers.add_parser('insar-delay',
                                               help=('Plot InSAR delay '
                                                     'computed by pysarts'))
    insar_delay_parser.add_argument('-m', '--master-date',
                                    action='store',
                                    default=None,
                                    required=True,
                                    help='Master date to plot delay for')
    insar_delay_parser.add_argument('-s', '--slave-date',
                                    action='store',
                                    default=None,
                                    required=True,
                                    help='Slave date to plot delay for')
    insar_delay_parser.add_argument('-o', '--output',
                                    action='store',
                                    default=None,
                                    help='Output file name')
    insar_delay_parser.set_defaults(func=_plot_insar_delay)

    lwc_parser = subparsers.add_parser('lwc',
                                       help=('Plot estimated liquid water content'))
    lwc_parser.add_argument('-d', '--date',
                            action='store',
                            required=True)
    lwc_parser.add_argument('-o', '--output',
                            default=None,
                            action='store',
                            help=('Output file name'))
    lwc_parser.set_defaults(func=_plot_lwc)

    args = parser.parse_args()
    os.chdir(args.directory)

    COAST_DETAIL = args.coast_detail
    FIGSIZE = args.figsize
    DPI = args.dpi

    config.load_from_yaml('config.yml')
    logging.basicConfig(level=config.LOG_LEVEL)

    args.func(args)
