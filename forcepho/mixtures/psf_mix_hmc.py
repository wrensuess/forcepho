#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np

try:
    import jax.numpy as jnp
    from jax import random, lax

    import numpyro
    import numpyro.distributions as dist
except(ImportError):
    pass


__all__ = ["psf_model", "psf_prediction", "smooth_psf"]


def smooth_psf(image, sigma):
    """Smooth a PSF image with a gaussian of a given sigma before fitting it.
    This can be useful to handle the outer, broad gaussians in GM mixtures of
    sersic profiles.

    The final convolution is :math:`\sum_i \sum_j S \, G_i \, S^T * P_j` where S
    is a shape & rotation matrix, and the goal here is to construct different
    {P_j}_i for each G_i

    The idea will be to fit to a PSF image smoothed by G_i, using a smaller number
    of components, and then deconvolve the smoothed fit by subtracting the width
    of G_i from the (diagonals of) the fitted Gaussian covariance matrix.

    Thus we need a method to smooth the PSFs by G_i
    """
    raise NotImplementedError


def psf_prediction(xpix, ypix, x=0, y=0,
                   a=1, weight=1,
                   sx=3, sy=3, rho=0,
                   bg=0):
    """Predict the flux for a GMM model.  This method should match, in both math
    and parameter names, the specification of the model in `twod_model`.  Note
    that vectors can be supplied for any of the parameters to implement a
    Gaussian mixture (where scalar parameters are repeated for as many Gaussians
    as necessary.)
    """
    norm = a * weight / (2 * np.pi * sx * sy * jnp.sqrt(1 - rho**2))
    dx = (xpix[:, None] - x) / sx
    dy = (ypix[:, None] - y) / sy
    exparg = -0.5 / (1 - rho**2) * (dx**2 + dy**2 - 2 * rho * dx * dy)
    mu = norm * jnp.exp(exparg)
    mu = jnp.sum(mu, axis=-1) + bg

    return mu


def psf_model(image=None, xpix=None, ypix=None, unc=1, ngauss=1, ngauss_neg=0,
              afix=None, amax=2, dcen=2, smax=[10], smin=0.6, maxbg=0):
    """numpyro model for a 2d mixture of gaussians with optional constant
    background component. Assumes PSF is (roughly) centered in the supplied
    image (i.e. x, y \approx mean(xpix), mean(ypix))

    Parameters
    ----------
    image : ndarray of shape (npix,)
        flux values

    xpix : ndarray of shape (npix,)
        x-coordinate of pixels

    ypix : ndarray of shape (npix,)
        y-coordinate of pixels

    ngauss : int
        number of Gaussian components

    afix : float or None
        If supplied, the *fixed* total combined amplitude of the gaussian
        mixture.

    amax : float
        If the amplitude is not fixed, this gives the upper limit on the total
        combined amplitude of the mixture

    dcen : float
        1-sigma width of the positional priors, in number of pixels from the
        mean pixel value in each dimension.

    smax : sequence of floats
        The maximum number of pixels for the width of the Gaussian in each
        dimension.  Note that if `ngauss` > 1, this can be a sequence

    smin : float, default 0.7
        The minimum number of pixels for the width of a gaussian; this keeps the
        gaussians from getting 'way too far' into the undersampled regime.

    maxbg : float, optional
        If > 0, gives the upper and negative lower limit on a constant
        background component, i.g. `-maxbg` < `bg` < `maxbg`
    """
    # TODO: implement second order correction?
    # TODO: add a negative gaussian option?

    # Assume centers near center of image
    xcen = (xpix.max() + xpix.min()) / 2
    ycen = (ypix.max() + ypix.min()) / 2

    # Fix total amplitude?
    if afix:
        tot = afix
    else:
        tot = numpyro.sample("a", dist.Uniform(0.5, amax))
    # Fit a background?
    if maxbg > 0:
        bg = numpyro.sample("bg", dist.Uniform(-maxbg, maxbg))
    else:
        bg = 0.0
    # Dirichlet for the weights
    if ngauss > 1:
        # (weakly) identify the components
        concentration = jnp.linspace(2, 1, ngauss)
        w = numpyro.sample("weight", dist.Dirichlet(concentration))
    else:
        w = 1.0

    x = numpyro.sample("x", dist.Normal(xcen * jnp.ones(ngauss), dcen))
    y = numpyro.sample("y", dist.Normal(ycen * jnp.ones(ngauss), dcen))
    sx = numpyro.sample("sx", dist.Uniform(smin, jnp.array(smax)))
    sy = numpyro.sample("sy", dist.Uniform(smin, jnp.array(smax)))
    rho = numpyro.sample("rho", dist.Uniform(-0.5, 0.5 * jnp.ones(ngauss)))

    mu = psf_prediction(xpix, ypix, x=x, y=y, a=tot, weight=w, sx=sx, sy=sy, rho=rho, bg=bg)

    # --- add negative gaussians? ---
    if ngauss_neg > 0:
        ng = ngauss_neg
        smax_neg = np.max(smax) / 2
        xm = numpyro.sample("x_m", dist.Normal(xcen * jnp.ones(ng), dcen))
        ym = numpyro.sample("y_m", dist.Normal(ycen * jnp.ones(ng), dcen))
        sxm = numpyro.sample("sx_m", dist.Uniform(smin, jnp.ones(ng) * smax_neg))
        sym = numpyro.sample("sy_m", dist.Uniform(smin, jnp.ones(ng) * smax_neg))
        rhom = numpyro.sample("rho_m", dist.Uniform(-0.5, 0.5 * jnp.ones(ng)))
        mmin = amax / 2.
        wm = numpyro.sample("weight_m", dist.Uniform(0, jnp.ones(ng) * mmin))

        mum = psf_prediction(xpix, ypix, x=xm, y=ym, a=1.0, weight=wm, sx=sxm, sy=sym, rho=rhom)

        mu -= mum

    d = numpyro.sample("flux", dist.Normal(mu, unc), obs=image)


if __name__ == "__main__":

    if True:

        from .utils_hmc import Image, infer, display

        # --- TWO DIMENSIONS ---

        num_warmup, num_samples = 5000, 1000

        # --- Generate data ---
        nx, ny, amps, snr = 21, 21, np.array([100, 50]), 1000
        a, ngauss = np.sum(amps), len(amps)
        weight = amps / a
        unc = a/(snr * np.sqrt(nx * ny))
        y = (ny-1)/2 + 3 * np.linspace(-1, 1, ngauss)
        #rho = np.linspace(-0.5, 0.5, ngauss)
        rho = 0.5
        ypix, xpix = np.meshgrid(np.arange(ny), np.arange(nx))
        xpix, ypix = xpix.flatten(), ypix.flatten()
        truth = psf_prediction(xpix, ypix, x=(nx-1)/2., y=y, sx=3, sy=3,
                               a=a, weight=weight, rho=rho)
        image = Image(xpix, ypix, truth, unc, nx, ny, (nx-1)/2., (ny-1)/2.)

        # --- Fit data ---
        best, samples, mcmc = infer(psf_model, image=image.data,
                                    xpix=image.xpix, ypix=image.ypix,
                                    ngauss=ngauss, unc=image.unc,
                                    num_warmup=num_warmup, num_samples=num_samples)

        model = psf_prediction(image.xpix, image.ypix, **best)

        # --- display ---
        fig, axes = display(model, image)
        import arviz as az
        data = az.from_numpyro(mcmc)
        az.plot_trace(data, compact=True)
