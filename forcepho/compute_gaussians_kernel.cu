/* compute_gaussian_kernels.cu

This is the code to compute a Gaussian mixture likelihood and derivative
on the GPU.  Top-level code view:

Create chi^2 and d(chi2/dparam) accumulators in shared memory and zero them.

For each exposure:

    Create on-image Gaussians from on-sky parameters, put in shared memory
    	ImageGaussian[NGalaxy*GaussianPerGalaxy]

    For one pixel per thread (taking BlockSize steps):

    	Load Image Data
		Loop over all ImageGaussians:
		    Evaluate Gaussians to create Residual image, save it

		Compute local_chi2 from residual image for this pixel
		Reduce local_chi2 over warp; atomic_add result to shared mem

		Loop over Active Galaxy:
		    Loop over Gaussian in this Galaxy:
				Compute local_dchi_dp and accumulate
		    Reduce local_dchi_dp over warp and atomic_add to shared dchi_dp for galaxy
	    	
When done with all exposures, copy the accumulators to the output buffer.
*/
// Shared memory is arranged in 32 banks of 4 byte stagger

//=============================================================================== 


#include "header.hh"
#include "patch.cu"
#include "proposal.cu"


class ImageGaussian {
  public:
    // 6 Gaussian parameters
	float amp;
	float xcen; 
	float ycen;
	float fxx; 
	float fyy;
	float fxy; 
	
    // 15 Jacobian elements (Image -> Sky)
    float dA_dFlux;
    float dx_dAlpha;
    float dy_dAlpha;
    float dx_dDelta;
    float dy_dDelta;
    float dA_dQ;
    float dFxx_dQ;
    float dFyy_dQ;
    float dFxy_dQ;
    float dA_dPA;
    float dFxx_dPA;
    float dFyy_dPA;
    float dFxy_dPA;
    float dA_dSersic;
    float dA_drh;
};

// ======================  Code to Evaluate the Gaussians =========================


/// Compute the Model for one pixel from all galaxies; return the residual image
__device__ PixFloat ComputeResidualImage(float xp, float yp, PixFloat data, ImageGaussian * imageGauss, int n_gauss_total)
{
	PixFloat residual = data;
	
	//loop over all image gaussians g for all galaxies. 
	for (int i = 0; i < n_gauss_total; i ++){
		ImageGaussian *g = imageGauss+i;
		float dx = xp - g->xcen; 
		float dy = yp - g->ycen; 
		float vx = g->fxx * dx + g->fxy * dy;
		float vy = g->fyy * dy + g->fxy * dx;
		float exparg = dx*vx+dy*vy;
		if (exparg>MAX_EXP_ARG) continue;
		float Gp = exp(-0.5 * exparg);

		// Here are the second-order corrections to the pixel integral
		float H = 1.0 + (vx*vx + vy*vy - g->fxx - g->fyy) / 24.0; 
		float C = g->amp * Gp * H; //count in this pixel. 
		
		residual -= C; 
	}
	return residual;
}

/// Compute the likelihood derivative for one pixel and one galaxy
__device__ void ComputeGaussianDerivative(float xp, float yp, float residual_ierr2, 
            ImageGaussian *gaussian, float * dchi2_dp, int n_gal_gauss) //pass in pointer to first gaussian for this galaxy. 
{
	for (int gauss = 0; gauss<n_gal_gauss; gauss++) {   //loop ovver all gaussians in this galaxy. 
		ImageGaussian *g = gaussian+gauss;
	
		float dx = xp - g->xcen; 
		float dy = yp - g->ycen; 
		float vx = g->fxx * dx + g->fxy * dy;
		float vy = g->fyy * dy + g->fxy * dx;
		float Gp = exp(-0.5 * (dx*vx + dy*vy));
	
		float H = 1.0 + (vx*vx + vy*vy - g->fxx - g->fyy) / 24.0; 
		float C = residual_ierr2 * g->amp * Gp * H;   
	
	    float dC_dA   = C / g->amp;
	    float dC_dx   = C*vx;
	    float dC_dy   = C*vy;
	    float dC_dfx  = -0.5*C*dx*dx;
	    float dC_dfy  = -0.5*C*dy*dy;
	    float dC_dfxy = -1.0*C*dx*dy;
	
	    float c_h = C / H;
	    dC_dx    -= c_h * (g->fxx*vx + g->fxy*vy) / 12.0;
	    dC_dy    -= c_h * (g->fyy*vy + g->fxy*vx) / 12.0;
	    dC_dfx   -= c_h * (1.0 - 2.0*dx*vx) / 24.0;
	    dC_dfy   -= c_h * (1.0 - 2.0*dy*vy) / 24.0;
	    dC_dfxy  += c_h * (dy*vx + dx*vy) / 12.0;
			 
	    //Multiply by Jacobian and add to dchi2_dp	
		dchi2_dp[0] += g->dA_dFlux * dC_dA ; 
		dchi2_dp[1] += g->dx_dAlpha * dC_dx + g->dy_dAlpha * dC_dy;
		dchi2_dp[2] += g->dx_dDelta * dC_dx + g->dy_dDelta * dC_dy;
		dchi2_dp[3] += g->dA_dQ  * dC_dA + dC_dfx * g->dFxx_dQ + dC_dfxy * g->dFxy_dQ + dC_dfy * g->dFyy_dQ;
		dchi2_dp[4] += g->dA_dPA * dC_dA + dC_dfx * g->dFxx_dPA + dC_dfxy * g->dFxy_dPA + dC_dfy * g->dFyy_dPA;
		dchi2_dp[5] += g->dA_dSersic * dC_dA;
		dchi2_dp[6] += g->dA_drh * dC_dA;	
	}
}


// =================== Code to prepare the Gaussians =======================

//NAM do we want this class, or should we make the convolve a method of PSFSourceGaussian?
typedef struct { 
    // 6 Gaussian parameters for sersic profile 
	float amp;
	float xcen;
	float ycen;
	float covar; //diagonal element of sersic profile covariance. covariance = covar * I. 
	matrix22 scovar_im; 
	
	//some distortions and astrometry specific to ??source??. 
	float flux; 
	float G; 
	float da_dn;
	float da_dr;
	matrix22 CW; 
	matrix22 T; 
	matrix22 dT_dq;
	matrix22 dT_dpa;
} PixGaussian; 

__device__ void  GetGaussianAndJacobian(PixGaussian & sersicgauss, PSFSourceGaussian & psfgauss, ImageGaussian & gauss){
	
	sersicgauss.scovar_im = sersicgauss.covar * sersicgauss.T.AAt(); 
		
	matrix22 covar = sersicgauss.scovar_im + matrix22(psfgauss.Cxx, psfgauss.Cxy, psfgauss.Cxy, psfgauss.Cyy); 
	matrix22 f = covar.inv(); 
	float detF = f.det(); 
	
	gauss.fxx = f.v11; 
	gauss.fxy = f.v21; 
	gauss.fyy = f.v22; 
	
	gauss.xcen = sersicgauss.xcen + psfgauss.xcen; 
	gauss.ycen = sersicgauss.ycen + psfgauss.ycen; 
	
	gauss.amp = sersicgauss.flux * sersicgauss.G * sersicgauss.amp * psfgauss.amp * sqrt(detF) / (2.0 * math.pi) ;

	//now get derivatives of F
	matrix22 dSigma_dq  = sersicgauss.covar * (sersicgauss.T * sersicgauss.dT_dq.T()  + sersicgauss.dT_dq  * sersicgauss.T.T() ) ; 
	matrix22 dSigma_dpa = sersicgauss.covar * (sersicgauss.T * sersicgauss.dT_dpa.T() + sersicgauss.dT_dpa * sersicgauss.T.T() ) ; 
	
	matrix22 dF_dq      = -matrix22::ABA (f, dSigma_dq);  // F *  dSigma_dq * F
	matrix22 dF_dpa     = -matrix22::ABA (f, dSigma_dpa); // F * dSigma_dpa * F
	
	float ddetF_dq   = detF *  (covar * dF_dq).trace(); 
	float ddetF_dpa  = detF * (covar * dF_dpa).trace(); 
	
	// Now get derivatives with respect to sky parameters
    gauss.dA_dQ      = gauss.amp / (2.0 * detF) * ddetF_dq;  
    gauss.dA_dPA     = gauss.amp / (2.0 * detF) * ddetF_dpa;  
    gauss.dA_dFlux   = gauss.amp / sersicgauss.flux; 
    gauss.dA_dSersic = gauss.amp / sersicgauss.amp * sersicgauss.da_dn;
    gauss.dA_drh     = gauss.amp / sersicgauss.amp * sersicgauss.da_dr;
	
	gauss.dx_dAlpha = sersicgauss.CW.v11; 
	gauss.dy_dAlpha = sersicgauss.CW.v21; 
	
	gauss.dx_dDelta = sersicgauss.CW.v12;
	gauss.dy_dDelta = sersicgauss.CW.v22; 
	
	gauss.dFxx_dQ = dF_dq.v11;
	gauss.dFyy_dQ = dF_dq.v22;
	gauss.dFxy_dQ = dF_dq.v21; 

	gauss.dFxx_dPA = dF_dpa.v11;
	gauss.dFyy_dPA = dF_dpa.v22;
	gauss.dFxy_dPA = dF_dpa.v21; 
}


__device__ void CreateImageGaussians(Patch * patch, Source * sources, int exposure, ImageGaussian * imageGauss) {
	
	__shared__ int band, psfgauss_start, n_psf_per_source, n_radii, n_gal_gauss; 
	__shared__ float G, crpix[2], crval[2]; 
	
	
	if ( threadIdx.x == 0 ){
	    band = blockIdx.x;   // This block is doing one band
		psfgauss_start = patch->psfgauss_start[exposure];
		G = patch->G[exposure]; 
	
		crpix[0] = patch->crpix[2*exposure];  crpix[1] = patch->crpix[2*exposure + 1];  
		crval[0] = patch->crval[2*exposure];  crval[1] = patch->crval[2*exposure + 1]; 
	
		n_psf_per_source = patch->n_psf_per_source[band]; //constant per band. 
		n_radii = patch->n_radii;
	    n_gal_gauss = patch->n_sources * n_psf_per_source;
	    // TODO: Consider use of __constant__ variables
	}
	
	__syncthreads();
	

	for (int tid = threadIdx.x; tid < n_gal_gauss; tid += blockDim.x) {
        int g = tid / n_psf_per_source;       // Source number
		int p = tid - g * n_psf_per_source;   // Gaussian number
		
		Source *galaxy = sources+g; 	
		PSFSourceGaussian *psfgauss = patch->psfgauss+psfgauss_start + p; 
		PixGaussian	sersicgauss; 
	
	    // Do the setup of the transformations		
		//Get the transformation matrix and other conversions
		matrix22 D, R, S; 
		
		int d_cw_start = 4 * (patch->n_sources * exposure + g); 
		D  = matrix22(patch->D+d_cw_start);		
		R.rot(galaxy->pa); 
		S.scale(galaxy->q); 
	
		//And its derivatives with respect to scene parameters
		matrix22 dS_dq, dR_dpa;
		dS_dq.scale_matrix_deriv(galaxy->q);
		dR_dpa.rotation_matrix_deriv(galaxy->pa);
				
		float smean[2]; 
		smean[0] = galaxy->ra  - crval[0];
		smean[1] = galaxy->dec - crval[1]; 
		sersicgauss.CW = matrix22(patch->CW+d_cw_start);
	    matrix22::Av(sersicgauss.CW, *smean); //multiplies CW (2x2) by smean (2x1) and stores result in smean. 
		
		int s = psfgauss->sersic_radius_bin; 
		sersicgauss.xcen = smean[0] + crpix[0]; 
		sersicgauss.ycen = smean[1] + crpix[1]; 
		sersicgauss.covar = patch->rad2[s]; 
		sersicgauss.amp   = galaxy->mixture_amplitudes[s]; 
		sersicgauss.da_dn = galaxy->damplitude_dnsersic[s];
		sersicgauss.da_dr = galaxy->damplitude_drh[s] ; 
		sersicgauss.flux = galaxy->fluxes[band]; 		//pull the correct flux from the multiband array
		sersicgauss.G = G; 
		sersicgauss.T = D * R * S; 
		sersicgauss.dT_dq  = D * R * dS_dq; 
		sersicgauss.dT_dpa = D * dR_dpa * S; 

    	GetGaussianAndJacobian(sersicgauss, *psfgauss, imageGauss[g * n_psf_per_source + p]);
	}
}
	
	

// ===================== Helper class for accumulating the results ========

class Accumulator {
  public:
    float chi2;
    float dchi2_dp[NPARAMS*MAXSOURCES]; 
        //OPTION: Figure out how to make this not compile time.

    Accumulator() {
        chi2 = 0.0;
        for (int j=0; j<NPARAMS*MAXSOURCES; j++) dchi2_dp[j] = 0.0;
    }
    ~Accumulator() { }

    void warpReduceSum(float *answer, float input) {
        input += __shfl_down(input, 16);
        input += __shfl_down(input,  8);
        input += __shfl_down(input,  4);
        input += __shfl_down(input,  2);
        input += __shfl_down(input,  1);
        if (threadIdx.x&31==0) atomicAdd_block(answer, input);
    }
    
    // Could put the Reduction code in here
    void SumChi2(float _chi2) { warpReduceSum(&chi2, _chi2); }
    void SumDChi2dp(float *_dchi2_dp, int gal) { 
        for (int j=0; j<NPARAMS; j++) 
            warpReduceSum(dchi2_dp+NPARAMS*gal+j, _dchi2_dp[j]); 
    }

    /// This copies this Accumulator into another memory buffer
    inline void store(float *pchi2, float *pdchi2_dp, int nActive) {
        if (threadIdx.x==0) *pchi2 = chi2;
        for (int j=threadIdx.x; j<nActive*NPARAMS; j+=blockDim.x)
            pdchi2_dp[j] = dchi2_dp[j];
    }

    inline void addto(Accumulator &A, int nActive) {
        if (threadIdx.x==0) chi2 += A.chi2;
        for (int j=threadIdx.x; j<nActive*NPARAMS; j+=blockDim.x)
            dchi2_dp[j] += A.dchi2_dp[j];
    }

    void coadd_and_sync(Accumulator *A, int nAcc, int nActive) {
        for (int n=1; n<nAcc; n++) addto(A[n], nActive);
        __syncthreads();
    }
};

// ================= Primary Proposal Kernel ========================


/// We are being handed pointers to a Patch structure, a Proposal structure,

/// a scalar chi2 response, and a vector dchi2_dp response.
/// The proposal is a pointer to Source[n_active] sources.
/// The response is a pointer to [band][MaxSource] Responses.

__global__ void EvaluateProposal(void *_patch, void *_proposal, 
                                 void *pchi2, void *pdchi2_dp) {
    // Get the patch set up
    Patch *patch = (Patch *)_patch;  

    // The Proposal is a vector of Sources[n_active]
    Source *sources = (Source *)_proposal;

    // Create And Zero Accumulators
    __shared__ Accumulator accum[NUMACCUMS]();
    float dchi2_dp[NPARAMS];   // This holds the derivatives for one galaxy

    // Now figure out which one this thread should use
    // OPTION: 32 is the warp size; perhaps use a built-in name
    int threads_per_accum = ceilf(blockDim.x/32/NUMACCUMS)*32;    
    int accumnum = threadIdx.x / threads_per_accum;  // We are accumulating each warp separately. 
	
    int thisband = blockIdx.x;   // This block is doing one band
    // TODO: Would this be better as a #define?  I.e., perhaps the blockIdx is faster/lighter

    // Allocate the ImageGaussians for this band (same number for all exposures)
    __shared__ int n_gal_gauss;   // Number of image gaussians per galaxy
    __shared__ ImageGaussian *imageGauss; // [source][gauss]
    if (threadIdx.x==0) 
        n_gal_gauss = patch->n_psf_per_source[thisband];
        imageGauss = (ImageGaussian *)malloc(
                sizeof(ImageGaussian)*n_gal_gauss * patch->n_sources);
        // The claim is that this malloc returns shared memory because the 
        // target pointer is shared.
    __syncthreads();   // Have to get this malloc done

    // Loop over Exposures
    for (int e = 0; e < patch->band_N[thisband]; e++) {
        int exposure = patch->band_start[thisband] + e;
		int start_psf_gauss = patch->psfgauss_start[exposure];

        CreateImageGaussians(patch, sources, exposure, imageGauss);

		__syncthreads();
	
		for (int p = threadIdx.x ; p < patch->exposure_N[exposure]; p += blockDim.x) {
		    int pix = patch->exposure_start[exposure] + p;

            // Get the data and compute the model for this one pixel
		    float xp = patch->xpix[pix];
		    float yp = patch->ypix[pix];
		    PixFloat data = patch->data[pix];
		    PixFloat ierr = patch->ierr[pix];
		    PixFloat residual = ComputeResidualImage(xp, yp, data, imageGauss, n_gal_gauss * patch->n_sources); 
            patch->residual[pix] = residual;

            // Compute chi2 and accumulate it
            residual *= ierr;   // Form residual/sigma, which is chi
		    float chi2 = residual*residual;
		    accum[accumnum].SumChi2(chi2);
            residual *= ierr;   // We want res*ierr^2 for the derivatives
	    
		    // Now we loop over Sources and compute the derivatives for each
		    for (int gal = 0; gal < patch->n_sources; gal++) {
				for (int j=0; j<NPARAMS; j++) dchi2_dp[j]=0.0;
				ComputeGaussianDerivative(xp, yp, residual, 
                        imageGauss+gal*n_gal_gauss, dchi2_dp, n_gal_gauss);  
				accum[accumnum].SumDChi2dp(dchi2_dp, gal);
		    }
		}
	__syncthreads();
    }

    // Now we're done with all exposures, but we need to sum the Accumulators
    // over all warps.
    accum[0].coadd_and_sync(accum, NUMACCUMS, patch->n_sources);
    Response *r = (Response *)pdchi2_dp;
    accum[0].store((float *)pchi2, &(pdchi2_dp[thisband].dchi2_dparam), patch->n_sources);
    return;
}
