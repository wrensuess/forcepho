// This file contains numerical limits that we have to hardwire to 
// ease fixed-sized allocations on the GPU

// The maximum number of bands we're allowed to use
#define MAXBANDS 30     

// The maximum number of active sources that the GPU can use
#define MAXSOURCES 30   

// The number of on-sky parameters per band that yield derivatives
#define NPARAMS 7
// NOTE: Changing this *also* requires changing the structure of 
// the ImageGaussian class and the computation of the derivatives.

// The maximum square distance in a Gaussian evaluation before we no-op.
// Note that this refers to Y in exp(-0.5*Y)
#define MAX_EXP_ARG 36.0

// The number of separate accumulators in each GPU block.
// Using more will consume more memory, but may avoid contention
// in atomicAdd's between warps.
#define NUMACCUMS 1


#define MAXRADII 10


// Shared memory in each GPU block is limited to 48 KB, which is 12K floats.
// We have a handful of single variables, and then the big items are:
// The accumulators are NUMACCUMS*(NPARAMS*MAXSOURCES+1) shared floats 
// The ImageGaussians are n_psf_per_source*n_sources*21 shared floats,
// so this is bounded by n_psf_per_source*MAXSOURCES*21.

// If n_psf ~ 20, then the memory per source is 20*21 for the gaussians,
// and only 7*NUMACCUMS for the accumulators.


