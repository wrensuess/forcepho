#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""fitting.py

Routines for fitting a Scene model to data, using older CPU-based
likelihood methods.
"""

import sys, os, time
from functools import partial as argfix
import numpy as np

from .likelihood import lnlike_multi, negative_lnlike_multi
from .model import ConstrainedTransformedPosterior as Posterior
from .utils import make_statscat, make_chaincat, extract_block_diag
from .region import CircularRegion


__all__ = ["Result", "run_lmc", "run_opt",
           "run_pymc3", "run_dynesty", "run_hmc"]


class Result(object):
    """A simple namespace for storing information about a run.
    """

    def __init__(self, filename=None, **kwargs):
        if filename:
            self.read_from_h5(filename)
            try:
                self.reconstruct()
            except:
                pass
        for k, v in kwargs.items():
            setattr(self, k, v)

    def fill(self, region, active, fixed, model,
             bounds=None, patchID=None, step=None, stats=None):
        """
        Parameters
        ----------
        region : a region.Region object
            The region defining the patch; its parameters will be added to the
            result.

        active : structured ndarray
            The active sources and their starting parameters.

        fixed : structured ndarray
            The fixed sources and their parameters.

        model : a model.PosteriorModel object
            Must contain `proposer.patch` and `scene` attributes

        bounds : optional
            If given, a structured ndarrray of lower and upper bounds for
            each parameter of each source.

        patchID : optional
            An integer giving the unique patch ID.

        step : optional
            If supplied, a littlemcmc NUTS step obect.  this contains the covariance matrix

        stats : optional
            If supplied, a littlemcmc stats object.

        Returns
        -------
        qcat : structured ndarray
            A structured array of the parameter values in the last sample of the chain.

        block_covs : ndarray of shape (N_source, N_param, N_param)
            The covariance matrices for the sampling potential, extracted as block
            diagonal elements of the full N_source * N_param square covariance
            array.  Note that it is in the units of the transformed, unconstrained
            sampling parameters.  If the prior bounds change, the covariance matrix
            is no longer valid (or must be retransformed)
        """
        # shortcuts
        patch = model.proposer.patch
        scene = model.scene
        bands = np.array(patch.bandlist, dtype="S")  # Bands actually present in patch
        shapenames = np.array(scene.sources[0].SHAPE_COLS, dtype="S")
        ref = np.array(patch.patch_reference_coordinates)
        block_covs = None

        # --- Header ---
        self.patchID = patchID
        self.reference_coordinates = ref
        self.bandlist = bands
        self.shapenames = shapenames

        # --- basic info ---
        self.ncall = model.ncall
        self.npix = patch.npix
        self.nexp = len(patch.epaths)
        self.exposures = np.array(patch.epaths, dtype="S")

        # --- region, active, fixed ---
        for k, v in region.__dict__.items():
            setattr(self, k, v)
        self.active = np.array(active)
        if fixed is not None:
            self.fixed = np.array(fixed)

        # --- chain as structured array ---
        self.chaincat = make_chaincat(self.chain, self.bandlist, self.active,
                                      self.reference_coordinates, shapes=self.shapenames)

        # --- covariance ---
        if step is not None:
            try:
                self.cov = np.array(step.potential._cov.copy())
            except(AttributeError):
                self.cov = np.diag(step.potential._var.copy())
            if stats is not None:
                self.stats = make_statscat(stats, step)

            n_param = len(bands) + len(shapenames)
            block_covs = extract_block_diag(self.cov, n_param)

        # --- priors ---
        if bounds is not None:
            self.bounds = bounds

        # --- last position as structured array ---
        # FIXME: do this another way; use get_sample_cat with iteration = -1
        #qlast = self.chain[-1, :]
        #scene.set_all_source_params(qlast)
        #patch.unzerocoords(scene)
        #for i, source in enumerate(scene.sources):
        #    source.id = active[i]["source_index"]
        #qcat = scene.to_catalog(extra_cols=["source_index"])
        #qcat["source_index"][:] = active["source_index"]
        #self.final = qcat
        qcat = self.get_sample_cat(-1)
        self.final = qcat

        return qcat, block_covs

    def get_sample_cat(self, iteration):
        #dtype_sample = np.dtype([desc[:2] for desc in self.chaincat.dtype.descr])
        #sample = np.zeros(self.n_active, dtype=dtype_sample)
        sample = self.active.copy()
        for d in sample.dtype.names:
            if d in self.chaincat.dtype.names:
                try:
                    sample[d] = self.chaincat[d][:, iteration]
                except(IndexError):
                    sample[d] = self.chaincat[d]
        return sample

    def dump_to_h5(self, filename):
        import h5py
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with h5py.File(filename, "w") as out:
            for name, value in vars(self).items():
                if type(value) == np.ndarray:
                    out.create_dataset(name, data=value)
                else:
                    try:
                        out.attrs[name] = value
                    except:
                        print("could not save {} with value {} to {}".format(name, value, filename))

    def read_from_h5(self, filename):
        import h5py
        out = self
        with h5py.File(filename, "r") as f:
            for k, v in f.items():
                setattr(out, k, f[k][:])
            for k, v in f.attrs.items():
                setattr(out, k, v)

    def reconstruct(self):
        self.bands = [b.decode("utf") for b in self.bandlist]
        self.shape_cols = [s.decode("utf") for s in self.shapenames]
        self.region = CircularRegion(self.ra, self.dec, self.radius)
        if not hasattr(self, "chaincat"):
            self.chaincat = make_chaincat(self.chain, self.bands, self.active,
                                          self.reference_coordinates, shapes=self.shape_cols)


    def show_chain(self, source_idx=0, bandlist=None, axes=None,
                   span=0.999999426697, post_kwargs=dict(alpha=0.5, color="royalblue"),
                   truth=None, truth_kwargs=dict(linestyle="--", color="tomato")):
        if bandlist is None:
            bandlist = self.bands
        cols = bandlist + self.shape_cols
        q = 100 * np.array([0.5 - 0.5 * span, 0.5 + 0.5 * span])
        for i, col in enumerate(cols):
            ax = axes.flat[i]
            xx = self.chaincat[source_idx][col]
            lim = np.percentile(xx, list(q))
            if self.bounds is not None:
                lim = self.bounds[source_idx][col]

            ax.plot(xx, **post_kwargs)
            ax.set_ylim(*lim)
            ax.set_ylabel(col)
            if truth is not None:
                ax.axhline(truth[col], **truth_kwargs)
        return ax


def priors(scene, stamps, npix=2.0):
    # --------------------------------
    # --- Priors ---
    plate_scale = np.abs(np.linalg.eigvals(np.linalg.inv(stamps[0].dpix_dsky)))
    rh_range = np.array(scene.sources[0].rh_range)
    sersic_range = np.array(scene.sources[0].sersic_range)

    lower = [s.nband * [0.] +
             [s.ra - npix * plate_scale[0], s.dec - npix * plate_scale[1],
              0.3, -np.pi / 1.5, sersic_range[0], rh_range[0]]
             for s in scene.sources]
    upper = [(np.array(s.flux) * 10).tolist() +
             [s.ra + npix * plate_scale[0], s.dec + npix * plate_scale[1],
              1.0, np.pi / 1.5, sersic_range[-1], rh_range[-1]]
             for s in scene.sources]
    upper = np.concatenate(upper)
    lower = np.concatenate(lower)
    fluxes = np.array([s.flux for s in scene.sources])
    scales = np.concatenate([f.tolist() + [plate_scale[0], plate_scale[1], 0.1, 0.1, 0.1, 0.01]
                             for f in fluxes])

    return scales, lower, upper


def run_lmc(model, q, n_draws, adapt=False, full=False, weight=10,
            warmup=[10], trace=None, z_cov=None, progressbar=False,
            random_seed=0xDEADBEEF, discard_tuned_samples=True):
    """Use the littlemcmc barebones NUTS algorithm to sample from the
    posterior.

    Parameters
    ----------
    model : The model object (instance of `forcepho.model.Posterior`)
        This should have the methods `lnprob_and_grad` and `transform()`

    q : ndarray of shape (ndim,)
        The starting position in the constrained parameter space q

    n_draws : int
        Number of posterior samples

    warmup: list of ints
        Number of iterations in each warmup-phase, used to tune the step-size.

    z_cov : optional, ndarray of shape (ndim, ndim)
        The mass matrix, in the unconstrained parameter space `z`

    full : bool, optional (default, False)
        Whether to use the full covariance matrix instead of the diagonal.

    adapt : bool, optional (default, False)
        If true, use a diagonol potential (mass_matrix) that adapts at every
        iteration for the first warmup round. Otherwise the covariance matrix
        will have off diagonal elements and only adapt between warmup rounds.

    Returns
    -------
    chain : ndarray of shape (n_draws, n_dim)
        The posterior samples in the parameter space `q` (i.e. not the sampling
        space `z`)

    step : littlemcmc.nuts.NUTS instance
        The stepper.  See step.potential._cov for the covariance (mass) matrix
        of the step, in `z` units

    stats : list of dicts
        Statistics for each sample
    """

    from littlemcmc.sampling import _sample as sample_one
    from littlemcmc import NUTS

    if model.transform is not None:
        start = model.transform.inverse_transform(q)
    else:
        start = q.copy()
    n_dim = len(start)

    # --- Burn-in windows with step tuning ---
    t = time.time()
    #z_cov, trace = warmup_rounds(warmup, model)

    # --- production run ---
    potential = get_pot(n_dim, adapt=adapt, full=full, weight=weight,
                        init_mean=start, init_cov=z_cov, trace=trace)
    step = NUTS(logp_dlogp_func=model.lnprob_and_grad,
                model_ndim=n_dim, potential=potential)
    trace, stats = sample_one(logp_dlogp_func=model.lnprob_and_grad,
                              model_ndim=n_dim, start=start, step=step,
                              draws=n_draws, tune=warmup[0],
                              discard_tuned_samples=discard_tuned_samples,
                              progressbar=progressbar,
                              chain=0, random_seed=random_seed)
    tsample = time.time() - t

    if model.transform is not None:
        chain = model.transform.transform(trace.T)
    else:
        chain = trace.T

    result = Result()
    result.ndim = len(q)
    result.starting_position = q.copy()
    result.chain = chain
    result.ncall = model.ncall
    result.wall_time = tsample

    return result, step, stats


def warmup_rounds(warmup, model):
    # for iiter, n_iterations in enumerate(warmup):
    #    if (iiter == 0) & adapt_one:
    #        adapt = True
    #    else:
    #        adapt = False
    #    potential = get_pot(init_cov=z_cov, n_dim=n_dim, trace=trace)
    #    step = NUTS(logp_dlogp_func=model.lnprob_and_grad,
    #                model_ndim=n_dim, potential=potential)
    #    trace, tstats = sample_one(logp_dlogp_func=model.lnprob_and_grad,
    #                               model_ndim=n_dim, start=start, step=step,
    #                               draws=2, tune=n_iterations,
    #                               discard_tuned_samples=False,
    #                               progressbar=progressbar)
    #    start = trace[:, -1]
    #    if adapt:
    #        # Use tuned covariance matrix
    #        z_cov = np.diag(step.potential._var)
    #    else:
    #        # estimate from samples
    #        z_cov = None

    raise(NotImplementedError)


def get_pot(n_dim, init_mean=None, init_cov=None, trace=None,
            regular_variance=1e-3, adapt=True, full=False, weight=10):
    """Generate a full potential (i.e. a mass matrix) either using a supplied
    covariance matrix, a trace of samples, or the identity.

    Returns
    -------
    potential : littlemcmc.quadpotential.QuadPotential instance.
        If `tune` this is an instance of QuadPotentialDiagAdapt with zero mean.
        Otherwise, an instance of QuadPotentialFull, i.e. a static 2-d mass-matrix.
    """
    from littlemcmc import QuadPotentialFull, QuadPotentialFullAdapt, QuadPotentialDiagAdapt
    from scipy.linalg import cholesky

    if init_cov is not None:
        # use_supplied values
        cov = np.array(init_cov)
    elif (trace is None) and (init_cov is None):
        # Default no information
        cov = np.eye(n_dim)
    elif (trace is not None) and (init_cov is None):
        # Estimate from trace
        cov = np.cov(trace, rowvar=True)
        init_mean = np.mean(trace, axis=-1)

    ntimes = 0
    while ntimes < 5:
        try:
            _ = cholesky(cov, lower=True)
            break
        except(np.linalg.LinAlgError):
            cov[np.diag_indices_from(cov)] += regular_variance
            print(ntimes)
            ntimes += 1

    if full:
        init_mass = cov
        potential = QuadPotentialFullAdapt(n_dim, init_mean, init_mass, weight)
    else:
        init_mass = np.diag(cov)
        potential = QuadPotentialDiagAdapt(n_dim, init_mean, init_mass, weight)

    return potential



def run_opt(model, q, jac=True, **extras):
    """Simple BFGS optimization using scipy.optimize
    """
    from scipy.optimize import minimize
    opts = {'ftol': 1e-5, 'gtol': 1e-5, 'factr': 10.,
            'disp':True, 'iprint': 1, 'maxcor': 20}
    callback = None

    if model.transform is not None:
        start = model.transform.inverse_transform(q)
    else:
        start = q.copy()


    t0 = time.time()
    scires = minimize(model.nll, start.copy(), jac=jac,  method='BFGS',
                      options=opts, bounds=None, callback=callback)
    tsample = time.time() - t0

    result = Result()
    result.ndim = len(q)
    result.starting_position = q.copy()
    result.chain = np.atleast_2d(scires.x)
    if model.transform is not None:
        result.chain = model.transform.transform(result.chain)

    result.lnp = -0.5 * scires.fun
    result.wall_time = tsample

    return result


def run_pymc3(model, q, lower=-np.inf, upper=np.inf,
              priors=None, nwarm=2000, niter=1000):
    """Run a pymc3 fit
    """
    import pymc3 as pm
    import theano.tensor as tt
    import theano
    theano.gof.compilelock.set_lock_status(False)
    from .model import LogLikeWithGrad

    logl = LogLikeWithGrad(model)
    pnames = model.scene.parameter_names
    assert len(upper) == len(lower) == len(pnames)
    t = time.time()
    with pm.Model() as opmodel:
        z0 = [pm.Uniform(p, lower=l, upper=u)
              for p, l, u in zip(pnames, lower, upper)]
        theta = tt.as_tensor_variable(z0)
        pm.DensityDist('likelihood', lambda v: logl(v), observed={'v': theta})
        trace = pm.sample(draws=niter, tune=nwarm, cores=1,
                          discard_tuned_samples=True)

    tsample = time.time() - t

    result = Result()
    result.ndim = len(q)
    result.starting_position = q.copy()
    result.chain = np.array([trace.get_values(n) for n in pnames]).T
    result.trace = trace
    result.ncall = model.ncall
    result.wall_time = tsample
    result.lower = lower
    result.upper = upper

    return result


def run_dynesty(model, q, lower=0, upper=1.0, nlive=50):
    """Run a dynesty fit of the model
    """
    import dynesty

    theta_width = (upper - lower)
    ndim = len(lower)

    def prior_transform(unit_coords):
        # now scale and shift
        theta = lower + theta_width * unit_coords
        return theta

    sampler = dynesty.DynamicNestedSampler(model.lnprob, prior_transform, ndim, nlive=nlive,
                                           bound="multi", method="rwalk", bootstrap=0)
    t0 = time.time()
    sampler.run_nested(nlive_init=int(nlive/2), nlive_batch=int(nlive),
                       wt_kwargs={'pfrac': 1.0}, stop_kwargs={"post_thresh":0.2})
    tsample = time.time() - t0

    dresults = sampler.results

    result = Result()
    result.ndim = ndim
    result.chain = dresults["samples"]
    result.lnp = dresults['logl']
    result.ncall = model.ncall
    result.wall_time = tsample
    result.lower = lower
    result.upper = upper

    return result, dresults


def run_hmc(model, q, scales=1.0, nwarm=0, niter=500, length=20,
            lower=-np.inf, upper=np.inf):

    from hmc import BasicHMC

    model.lower = lower
    model.upper = upper
    sampler = BasicHMC(model, verbose=False)
    sampler.ndim = len(q)
    sampler.set_mass_matrix(1 / scales**2)

    result = Result()
    eps = sampler.find_reasonable_stepsize(q * 1.0)
    use_eps = sampler.step_size * 2
    result.step_size = np.copy(use_eps)
    result.metric = scales**2

    if nwarm > 0:
        out = sampler.sample(q, iterations=nwarm, mass_matrix=1 / scales**2,
                             epsilon=use_eps, length=length,
                             sigma_length=int(length / 4),
                             store_trajectories=True)
        pos, prob, grad = out
        use_eps = sampler.find_reasonable_stepsize(pos)
        result.step_size = np.copy(use_eps)
        ncwarm = np.copy(model.ncall)
        model.ncall = 0

    out = sampler.sample(pos, iterations=niter, mass_matrix=1 / scales**2,
                         epsilon=use_eps, length=length,
                         sigma_length=int(length / 4),
                         store_trajectories=True)
    pos, prob, grad = out

    result.ndim = sampler.ndim
    result.starting_position = q.copy()
    result.chain = sampler.chain.copy()
    result.lnp = sampler.lnp.copy()
    result.lower = lower
    result.upper = upper
    result.trajectories = sampler.trajectories
    result.accepted = sampler.accepted
    result.ncall = (ncwarm, np.copy(model.ncall))

    return result


def run_hemcee(model, q, scales=1.0, nwarm=2000, niter=1000):
    """Deprecated, hemcee is broken
    """
    from hemcee import NoUTurnSampler
    from hemcee.metric import DiagonalMetric

    metric = DiagonalMetric(scales**2)
    sampler = NoUTurnSampler(model.lnprob, model.lnprob_grad, metric=metric)

    t = time.time()
    pos, lnp0 = sampler.run_warmup(q, nwarm)
    twarm = time.time() - t
    ncwarm = np.copy(model.ncall)
    model.ncall = 0
    t = time.time()
    chain, lnp = sampler.run_mcmc(pos, niter)
    tsample = time.time() - t
    ncsample = np.copy(model.ncall)

    result = Result()
    result.ndim = len(q)
    result.starting_position = q.copy()
    result.chain = chain
    result.lnp = lnp
    result.ncall = (ncwarm, ncsample)
    result.wall_time = (twarm, tsample)
    result.metric_variance = np.copy(metric.variance)
    result.step_size = sampler.step_size.get_step_size()

    return result


if __name__ == "__main__":

    from scipy.stats import norm
    from forcepho.model import BoundedTransform, CPUPosterior
    import matplotlib.pyplot as pl
    pl.ion()

    # --- dimension and bounds ---
    ndim = 30
    a = 1.5
    epsilon = 1e-2

    # --- Build the model ---
    def lnlike_gauss(theta, **extras):
        lnp = -0.5 * np.dot(theta, theta)
        lnp_grad = -theta
        return lnp, lnp_grad


    if a is not None:
        lower = np.zeros(ndim) - a
        upper = np.zeros(ndim) + a
        transform = BoundedTransform(lower, upper)
    else:
        transform = None
        upper = np.inf
        lower = -np.inf
    model = CPUPosterior(lnlike=lnlike_gauss, transform=transform)
    q = np.clip(np.random.normal(0, 1, size=(ndim,)), lower + epsilon, upper - epsilon)

    # --- Run the sampler ---
    chain, step, stats = run_lmc(model, q, n_draws=1000, HASGPU=False,
                                 warmup=[256, 512, 1024, 1024, 1024, 2048])


    if a is not None:
        from scipy.special import erf
        A = erf(a / np.sqrt(2))
    else:
        A = 1

    fig, ax = pl.subplots()
    hh = [ax.hist(chain[:, i], histtype="step", bins=np.linspace(-4, 4, 100), alpha=0.5, density=True)
          for i in range(ndim)]
    xx = np.linspace(-10, 10, 1000)
    ax.plot(xx, norm.pdf(xx) / A)
    pl.show()