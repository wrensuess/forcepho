import numpy as np

__all__ = ["PointSpreadFunction", "get_psf", "make_psf", "params_to_gauss"]


class PointSpreadFunction(object):
    """Gaussian Mixture approximation to a PSF.
    """

    def __init__(self, parameters=None, units='pixels'):
        if parameters is None:
            self.ngauss = 1
            self.covariances = np.array(self.ngauss * [[[1.,0.], [0., 1.]]])
            self.means = np.zeros([self.ngauss, 2])
            self.amplitudes = np.ones(self.ngauss)
        else:
            self.make_from_parameters(parameters)

        self.units = units

    def make_from_parameters(self, parameters):
        """Make psf from a structured array of parameters, of length `ngauss`
        """
        self.ngauss = len(parameters)
        cov = [np.array([[p["vxx"], p["vxy"]],[p["vxy"], p["vyy"]]])
               for p in parameters]
        self.covariances = np.array(cov)
        self.means = np.array([parameters["x"], parameters["y"]]).T
        self.amplitudes = parameters["amp"]

    def as_tuplelist(self):
        """Return the parameters of the gaussians in the PSF mixture as a list of tuples.  Each element of the list is a tuple of (a, x, y, cxx, cyy, cxy)
        """
        params = []
        for i in range(self.ngauss):
            cov = self.covariances[i][0,0]
            cxx, cxy, cyy = cov[0,0], cov[0,1], cov[1,1]
            amp = self.amplitudes[i]
            xcen, ycen = self.means[i]
            params.append((amp, xcen, ycen, cxx, cyy, cxy))
        return params
   

def get_psf(psfname=None, fwhm=1.0, psf_realization=0,
            ngauss=None, oversample=8, center=104):
    """Given a filename and some other choices, try to build and return a PSF.
    This supercedes `make_psf` and can work with newstyle hdf5 PSF data.

    :param psfname:
        Absolute path to a file contining PSF information

    :param psf_realization:
        Zero-based index for the PSF solution to be used (often multiple
        solutions are found for a given PSF mixture by starting from different
        conditions)

    :param ngauss, oversample, center:
        Necessary parameters for oldstyle PSFs in pickle files, which are keyed
        by `ngauss` and require knowledge of the central pixel and oversampling
        of the original PSF image.

    :param fwhm:
        If `psfname` is not given, the psf will be a single gaussian with this
        FWHM (in pixels).

    :returns psf:
        An instance of PointSpreadFunction
    """
    if psfname is not None:
        # oldstyle
        try:
            import pickle
            with open(psfname, 'rb') as pf:
                pdat = pickle.load(pf)

            if ngauss is None:
                ngauss = pdat.keys()[0]
            answer = pdat[ngauss][psf_realization]
            psf = make_psf(answer, oversample=oversample, center=center)
        # newstyle
        except:
            import h5py
            with h5py.File(psfname, "r") as pdat:
                psf_pars = pdat["parameters"][psf_realization]
            psf = PointSpreadFunction(psf_pars)

    else:
        psf = PointSpreadFunction()
        psf.covariances *= fwhm/2.355

    return psf


def make_psf(answer, **kwargs):

    psf = PointSpreadFunction()
    x, y, vx, vy, vxy, amps = params_to_gauss(answer, **kwargs)
    # Build the covariance matrices
    cov = [np.array([[xv, xyv],[xyv, yv]]) for xv, yv, xyv in zip(vx, vy, vxy)]
    psf.ngauss = len(x)
    psf.means = np.array([x, y]).T
    psf.amplitudes = amps
    psf.covariances = np.array(cov)

    return psf


def params_to_gauss(answer, oversample=8, start=0, center=504):
    """Convert the fitted parameters to the parameters used in the PSF gaussian mixture.

    :returns mux:
        The center x of each gaussian, in detector coordinates (i.e. not PSF
        image pixels, but actual detector pixels)

    :returns muy:
        The center y of each gaussian, in detector coordinates (i.e. not PSF
        image pixels, but actual detector pixels)

    :returns vx:
        The 0,0 entry of the covariance matrix (sigma_x^2) in (detector pixels)^2

    :returns vy:
        The 1,1 entry of the covariance matrix (sigma_y^2) in (detector pixels)^2

    :returns vxy:
        The 1,0 or 0,1 entry of the covariance matrix (rho * sigma_x * sigma_y)
        in (detector pixels)^2

    :returns amp:
        The amplitude of the gaussians
    """
    params = answer['fitted_params'].copy()
    ngauss = len(params) / 6
    params = params.reshape(ngauss, 6)
    # is this right?
    #TODO: work out zero index vs 0.5 index issues
    # need to flip x and y here
    mu = (params[:,1:3][:, ::-1] + start - center) / oversample
    sy = params[:, 3] / oversample
    sx = params[:, 4] / oversample
    vxy = params[:, 5] * sx * sy
    amp = params[:, 0]

    return mu[:, 0], mu[:, 1], sx**2, sy**2, vxy, amp


def mvn_pdf(pos_x, pos_y, params):
    amp, mu_x, mu_y, sigma_x, sigma_y, rho = params
    A = amp / (2 * np.pi * sigma_x * sigma_y * np.sqrt(1-rho**2))
    dx = (pos_x - mu_x) / sigma_x
    dy = (pos_y - mu_y) / sigma_y
    exparg = -0.5 / (1-rho**2) * (dx**2 + dy**2 - 2 * rho * dx * dy)
    return A * np.exp(exparg)


def mvn_pdf_2d(params, x_max, y_max):  # 0,1,...,x_max-1
    pos_x_range = np.arange(x_max)
    pos_y_range = np.arange(y_max)
    result = mvn_pdf(pos_x_range[:, None], pos_y_range[None, :], params)
    return result


def mvn_pdf_2d_mix_fn(num_mix, x_max, y_max):
    def ret_func(params):
        ans = np.zeros([x_max, y_max])
        for i in range(num_mix):
            ans += mvn_pdf_2d(params[(6*i):(6*i+6)], x_max, y_max)
        return ans
    return ret_func
