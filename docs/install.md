# Installation

## Requirements

The python packages that |Codename| requires are listed in `requirements.txt`

In addition, for GPU and multiproceessing capability the python packages will require CUDA and MPI installations (known to work with CUDA 10.1 and open-MPI).  You will also need an HDF5 installation.  These are often available on computing clusters as modules.

## Conda install

The easiest way to install is to create a conda environment, using the supplied `environment.yml` file.

```bash
git clone git@github.com:bd-j/forcepho.git
cd forcepho
conda env create -f environment.yml
conda activate fpho
python -m pip install .
```

## Clusters

### cannon

slurm script directives, also load these modules before installing
   ```bash
   module purge
   module load git/2.17.0-fasrc01
   module load intel/19.0.5-fasrc01 openmpi/4.0.1-fasrc01 hdf5/1.10.5-fasrc01
   module load cuda/10.1.243-fasrc01
   module load Anaconda3/5.0.1-fasrc01
   source activate fpho
   ```

### lux

### GPU details

MPS and Profiling

From the odyssey docs: While on GPU node, you can run `nvidia-smi` to get information about the assigned GPU

Not sure if it's necessary or how to enable MPS server.  On ascent one does

```bash
-alloc_flags "gpumps"
```

output.%h.%p

use `::KernelName:<int>` where `<int>` is the index of the kernel invocation that you want to profile

```bash
# detailed profiling of the kernel
jsrun -n1 -g1 -a1  nvprof --analysis-metrics -o /gpfs/wolf/gen126/scratch/bdjohnson/large_prof_metrics%h.%p.nvvp python run_patch_gpu_test_simple.py

# FLOP count
jsrun -n1 -g1 -a1  nvprof --kernels ::EvaluateProposal:1 --metrics flop_count_sp python run_patch_gpu_test_simple.py
```