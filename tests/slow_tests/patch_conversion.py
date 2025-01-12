'''
convert patch to forcepho stamp object and mini scene
'''

import os
import numpy as np
import h5py
import json

from forcepho.sources import Galaxy, Scene
from forcepho.stamp import PostageStamp
from forcepho import psf as pointspread

from astropy.wcs import WCS


__all__ = ["make_individual_stamp", "set_scene", "get_transform_mats", "patch_conversion",
           "zerocoords", "set_inactive"]


def make_individual_stamp(hdf5_file, filter_name, exp_name, psfpath=None, background=0.0):
    """Make a stamp object (including relevant metadata) for a certain exposure.
    
    Returns
    -------
    stamp: A Stamp() object
    """

    data = hdf5_file['images'][filter_name][exp_name]

    # get meta data about exposure
    dict_info = dict(zip(data.attrs.keys(), data.attrs.values()))

    # add image and uncertainty data to Stamp, flipping axis order
    stamp = PostageStamp()
    stamp.pixel_values = np.array(data['sci']).T - background
    stamp.ierr = 1.0 / np.array(data['rms']).T
    mask = np.array(data['mask']).T
    stamp.nx, stamp.ny = stamp.pixel_values.shape
    # note the inversion of x and y order in the meshgrid call here
    stamp.ypix, stamp.xpix = np.meshgrid(np.arange(stamp.ny), np.arange(stamp.nx))

    # masking bad pixels: set error to inf
    bad = ~np.isfinite(stamp.ierr) | (mask == 1.0)
    stamp.pixel_values[bad] = 0.0
    stamp.ierr[bad] = np.inf
    stamp.ierr = stamp.ierr

    # add WCS info to Stamp
    stamp.crpix = dict_info['crpix']
    stamp.crval = dict_info['crval']
    stamp.dpix_dsky = dict_info['dpix_dsky']
    stamp.scale = dict_info['scale']
    stamp.CD = dict_info['CD']
    stamp.W = dict_info['W']
    hdr = json.loads(data['header'][()])
    stamp.wcs = WCS(hdr)

    # add the PSF
    psfname = data['psf_name'][0].decode("utf-8")
    stamp.psf = pointspread.get_psf(os.path.join(psfpath, psfname))

    # add extra information
    stamp.photocounts = dict_info['phot']
    stamp.full_header = None
    stamp.filtername = filter_name
    stamp.band = hdf5_file['images'][filter_name].attrs['band_idx']

    return(stamp)


def set_scene(sourcepars, fluxpars, filters, splinedata=None, free_sersic=True):
    """Build a scene from a set of source parameters and fluxes through a set of filters.
    
    Returns
    -------
    scene: Scene object
    """
    sourcepars = sourcepars.astype(np.float)

    # get all sources
    sources = []
    for ii_gal in range(len(sourcepars)):
        gal_id, x, y, q, pa, n, rh = sourcepars[ii_gal]
        #print(x, y, type(pa), pa)
        s = Galaxy(filters=filters.tolist(), splinedata=splinedata, free_sersic=free_sersic)
        s.global_id = gal_id
        s.sersic = n
        s.rh = np.clip(rh, 0.05, 0.10)
        s.flux = fluxpars[ii_gal]
        s.ra = x
        s.dec = y
        s.q = np.clip(q, 0.2, 0.9)
        s.pa = np.deg2rad(pa)
        sources.append(s)

    # generate scene
    scene = Scene(sources)

    return(scene)


def get_transform_mats(source, wcs):
    """Get source specific coordinate transformation matrices CW and D
    """

    # get dsky for step dx, dy = 1, 1
    pos0_sky = np.array([source.ra, source.dec])
    pos0_pix = wcs.wcs_world2pix([pos0_sky], 1)[0]
    pos1_pix = pos0_pix + np.array([1.0, 0.0])
    pos2_pix = pos0_pix + np.array([0.0, 1.0])
    pos1_sky = wcs.wcs_pix2world([pos1_pix], 1)
    pos2_sky = wcs.wcs_pix2world([pos2_pix], 1)

    # compute dpix_dsky matrix
    [[dx_dra, dx_ddec]] = (pos1_pix-pos0_pix)/(pos1_sky-pos0_sky)
    [[dy_dra, dy_ddec]] = (pos2_pix-pos0_pix)/(pos2_sky-pos0_sky)
    CW_mat = np.array([[dx_dra, dx_ddec], [dy_dra, dy_ddec]])

    # compute D matrix
    W = np.eye(2)
    W[0, 0] = np.cos(np.deg2rad(pos0_sky[-1]))**-1
    D_mat = 1.0/3600.0*np.matmul(W, CW_mat)

    return(CW_mat, D_mat)


def zerocoords(stamps, scene, sky_zero=(53.0, -28.0)):
    """Reset (in-place) the celestial zero point of the image metadata and the source 
    coordinates to avoid catastrophic cancellation errors in coordinates when
    using single precision.
    
    Parameters
    ----------
    stamps: iterable
        A list of Stamp objects
    
    scene:
        A Scene object, where each source has the attributes `ra`, `dec`,
        `stamp_crvals`.
        
    sky_zero: optional, 2-tuple of float64
        The (ra, dec) values defining the new central coordinates.  These will
        be subtracted from the relevant source and stamp coordinates
    """
    zero = np.array(sky_zero)
    for source in scene.sources:
        source.ra -= zero[0]
        source.dec -= zero[1]
        new_crv = []
        for crv in source.stamp_crvals:
            new_crv.append(crv - zero)
        source.stamp_crvals = new_crv
    
    for stamp in stamps:
        stamp.crval -= zero


def set_inactive(scene, stamps, pad=5, nmax=0):
    # set outside sources to be fixed.
    for stamp in stamps:
        for source in scene.sources:
            x, y = stamp.sky_to_pix([source.ra, source.dec])
            good = ((x < stamp.nx + pad) & (x > -pad) &
                    (y < stamp.ny + pad) & (y > -pad))
            if ~good:
                source.fixed = True

    insources = [s for s in scene.sources if not s.fixed]
    fluxes = np.array([s.flux for s in insources])
    order = np.argsort(fluxes.max(axis=-1))
    use = order[-nmax:]
    use_sources = [insources[i] for i in use]
    microscene = Scene(use_sources)
    try:
        microscene.npsf_per_source = scene.npsf_per_source
    except(AttributeError):
        pass
    return microscene


def patch_conversion(patch_name, splinedata, psfpath, nradii=9,
                     use_bands=slice(None)):
    """Reads an HDF5 file with exposure data and metadata and source parameters 
    and returns a list of Stamp objects and a Scene object with appropriate
    attributes. This method determines source specific exposure metadata and
    attaches lists of this metadata (in the same order as the output stamp list)
    as attributes of the individual source objects in the Scene.

    Parameters
    ----------
    patch_name: string 
        The full patch to the HDF file containing the exposure and source
        infomation.

    splinedata: string 
        The full path to an HDF file containing information about the gaussian
        mixture approximations to Sersic profiles as a function of nsersic and
        rhalf
        
    psfpath: string
        Path to the directory containing the PSF gaussian mixtures referred to
        in the HDF file.
        
    nradii: optional, int, default: 9
        The number of Sersic radii (i.e. the number of circular gaussians used
        to approximate the Sersic profile)
        
    Returns
    -------
    
    stamps: list
        A list of Stamp objects, one for each exposure in this patch
        
    miniscene:
        A Scene object, containing a list of sources for this patch.
    """
    # read file
    hdf5_file = h5py.File(patch_name, 'r')

    # get filter list
    filter_list = hdf5_file['images'].attrs['filters'][use_bands]
    filter_list_list = filter_list.tolist()

    # create scene
    mini_scene = set_scene(hdf5_file['mini_scene']['sourcepars'][:],
                           hdf5_file['mini_scene']['sourceflux'][:, use_bands],
                           filter_list, splinedata=splinedata)

    # make list of stamps
    stamp_list = []
    stamp_filter_list = []
    for filter_name in filter_list:
        for exp_name in hdf5_file['images'][filter_name].attrs['exposures']:
            stamp = make_individual_stamp(hdf5_file, filter_name, exp_name, 
                                          psfpath=psfpath, background=0.0)
            stamp_list.append(stamp)
            stamp_filter_list.append(filter_name)

    # loop over sources to add additional information
    for ii_s in range(len(mini_scene.sources)):

        source = mini_scene.sources[ii_s]

        # set lists to be filled
        D_list = []
        psf_list = []
        CW_list = []
        crpix_list = []
        crval_list = []
        G_list = []
        find_list = []

        # loop over all stamps (i.e. filters and exposures) to add source specific information
        for s in stamp_list:
            wcs = s.wcs
            CW_mat, D_mat = get_transform_mats(source, wcs)
            CW_list.append(CW_mat)
            D_list.append(D_mat)
            psfs = nradii * [s.psf]
            psf_list.append(psfs)
            crpix_list.append(s.crpix)
            crval_list.append(s.crval)
            G_list.append(s.photocounts)
            find_list.append(filter_list_list.index(s.filtername))

        source.stamp_scales = D_list
        source.stamp_psfs = psf_list
        source.stamp_cds = CW_list
        source.stamp_crpixs = crpix_list
        source.stamp_crvals = crval_list
        source.stamp_zps = G_list
        source.stamp_filterindex = find_list

    # loop over stamps, count gaussian components in psf for each band
    npsf_list = []

    for filter_name in filter_list:
        idx_s = (np.array(stamp_filter_list) == filter_name)
        s = np.array(stamp_list)[idx_s][0]
        psfs = nradii * [s.psf]
        npsf = np.sum([p.ngauss for p in psfs])
        npsf_list.append(npsf)

    mini_scene.npsf_per_source = np.array(npsf_list, dtype=np.int16)

    return stamp_list, mini_scene



'''
# testing

# define path to PSF and filename of patch

base = "/Users/sandrotacchella/Desktop/patch_construction/"
psfpath = os.path.join(base, "psfs", "mixtures")
patch_name = os.path.join(base, "test_patch.h5")


# filename of spline data
splinedata = os.path.join(base, "data/sersic_mog_model.smooth=0.0150.h5")


# number of PSFs, place holder
nradii = 9


# convert patch into list of stamps and mini scene
list_of_stamps, mini_scene = patch_conversion(patch_name, splinedata, psfpath, nradii=nradii)

'''
