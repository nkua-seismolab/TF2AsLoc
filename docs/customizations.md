# Customizations

TF2AsLoc bundles third-party software with a few deliberate deviations from the stock distributions. They are all applied automatically when the Docker image is built (`Dockerfile.tf2asloc`) - nothing needs to be done manually - but they are documented here because they matter when debugging or when running outside the provided containers.

## GaMMA: `fork` → `spawn` multiprocessing

Stock [GaMMA](https://github.com/AI4EPS/GaMMA) hard-codes the `fork` multiprocessing start method on Linux. Forking a worker pool from TF2AsLoc's multi-threaded orchestrator can inherit a lock held by another thread at fork time and deadlock the pipeline (this froze multiple test runs before the patch was introduced).

The image build patches GaMMA's `utils` module to use the `spawn` context instead:

```dockerfile
RUN GAMMA_UTILS=$(python -c "import gamma.utils, inspect; print(inspect.getfile(gamma.utils))") \
    && sed -i 's/context = "fork"/context = "spawn"/' "$GAMMA_UTILS" \
    && grep -q 'context = "spawn"' "$GAMMA_UTILS"
```

With this patch, `gamma.ncpu > 1` is considered safe. If you install GaMMA yourself outside the provided image, apply the same change or keep `ncpu: 1`.

## GaMMA installed from source

The image installs GaMMA directly from the GitHub repository:

```dockerfile
RUN pip install --no-cache-dir "git+https://github.com/AI4EPS/GaMMA.git"
```

## BLAS/OpenMP thread caps

`docker-compose.yml` sets `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1` (and related variables) for the orchestrator container. NumPy/SciPy operations otherwise spin up multi-threaded native math libraries, and forking worker processes from a process with active native threads is another instance of the fork-after-threading hazard described above. The cap costs little: the heavy per-event workloads are already parallelized at the process level (`magnitude.workers`, `gamma.ncpu`).

## HypoInverse compiled from source

USGS distributes HypoInverse (hyp1.40) as Fortran source only. The image's builder stage downloads the official tarball and compiles it, with two adjustments to the stock makefile:

- the obsolete `g77` compiler is replaced with `gfortran`;
- the hardcoded NCSN install path is redirected to the build directory.

The resulting binary is installed as `/usr/local/bin/hypoinverse` in the final image.

## Orchestrator signal handling around forked pools

Worker pools are forked from the orchestrator process and would inherit its `SIGTERM` handler; forked children restore the default signal action so that pool shutdown behaves correctly during `docker compose down`.
