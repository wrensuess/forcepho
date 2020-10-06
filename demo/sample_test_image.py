#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""sample_test_image.py - Fit a scene to a test image, using littlemcmc
"""

import os, sys, argparse, time
import numpy as np
from astropy.io import fits
# import h5py

from forcepho.dispatcher import SuperScene
from forcepho.sources import Galaxy
from forcepho.proposal import Proposer
from forcepho.patches import JadesPatch

from forcepho.model import GPUPosterior, BoundedTransform
from forcepho.fitting import Result, run_lmc

from utils import Logger, rectify_catalog
from config_test import config

try:
    import pycuda
    import pycuda.autoinit
    HASGPU = True
except:
    print("NO PYCUDA")
    HASGPU = False

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--logging", action="store_true")
    parser.add_argument("--patch_dir", type=str, default="../output/")
    args = parser.parse_args()

    if args.logging:
        import logging
        logging.basicConfig(level=logging.DEBUG)
        logger = logging.getLogger(__name__)
    else:
        logger = Logger(__name__)

    # --- Wire the data --- (child)
    patcher = JadesPatch(metastore=config.metastorefile,
                         psfstore=config.psfstorefile,
                         pixelstore=config.pixelstorefile,
                         splinedata=config.splinedatafile,
                         return_residual=True)
    logger.info("Data loaded, HASGPU={}".format(HASGPU))

    # --- Get the patch dispatcher ---  (parent)
    cat, bands, chdr = rectify_catalog(config.raw_catalog)
    sceneDB = SuperScene(sourcecat=cat, bands=bands,
                         maxactive_per_patch=config.maxactive_per_patch,
                         maxradius=config.patch_maxradius,
                         target_niter=config.sampling_draws,
                         statefile=os.path.join(args.patch_dir, "superscene.fits"),
                         bounds_kwargs={})
    #sceneDB.bounds_catalog = make_bounds(sceneDB.sourcecat, bands)
    #sceneDB.covariance_matrices = init_covar(len(sceneDB.parameter_columns), sceneDB.n_sources)
    logger.info("Made SceneDB")
    error = None

    # --- Sample the patches ---
    while sceneDB.undone:
        # --- checkout a scene --- (parent)
        region, active, fixed = sceneDB.checkout_region()
        if active is None:
            continue
        patchID = "{:04.0f}".format(active["source_index"][0])
        logger.info("Checked out scene with {} active sources".format(len(active)))

        # --- Build patch --- (child)
        patcher.build_patch(region, None, allbands=bands)
        proposer, q = patcher.prepare(active=active, fixed=fixed)
        logger.info("Prepared Patch {}".format(patchID))

        # --- Get bounds, covariances, and sample --- (child)
        weight = max(10, active["n_iter"].min())
        bounds = sceneDB.bounds_catalog[active["source_index"]]
        lower, upper, cov = sceneDB.bounds_and_covs(active["source_index"],
                                                    bands=patcher.bandlist,
                                                    ref=patcher.patch_reference_coordinates)
        model = make_model(proposer, lower, upper)
        logger.info("Model made, sampling with covariance weight={}".format(weight))
        try:
            out, step, stats = run_lmc(model, q.copy(), config.sampling_draws,
                                       full=config.full_cov, z_cov=cov, adapt=True,
                                       weight=weight, warmup=config.warmup, progressbar=True)
        except(ValueError) as e:
            print("error with patchID = {}".format(patchID))
            print(active)
            print("starting position = {}".format(q))
            print("below lower = {}".format((q > lower).sum()))
            print("above upper = {}".format((q < upper).sum()))
            print("-------\n")
            fits.writeto("emergency_dump_active{}.fits".format(patchID), active, overwrite=True)

            error = e
            break

        # --- Deal with results --- (child/parent)
        logger.info("Sampling complete, preparing output.")
        final, covs = out.fill(region, active, fixed, model, bounds=bounds,
                                       step=step, stats=stats, patchID=patchID)
        logger.info("Checking region back in")
        sceneDB.checkin_region(final, fixed, config.sampling_draws, block_covs=covs, patchID=patchID)

        outfile = os.path.join(args.patch_dir, "patch{}_results.h5".format(patchID))
        logger.info("Writing to {}".format(outfile))
        out.dump_to_h5(outfile)

    sceneDB.writeout()

    if error is not None:
        raise(error)