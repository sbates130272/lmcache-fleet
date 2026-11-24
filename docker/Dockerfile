# syntax=docker/dockerfile:1
#
# The LMCache image this demo runs on: CPU-only, ~1.7 GB.
#
# This fleet never touches a GPU -- the servers run `--mode cpu` and there is
# no vLLM -- so the image carries no GPU runtime at all. Two things make that
# possible:
#   * lmcache's own code has no unguarded `import cupy / nvtx / cuda` on the
#     paths this demo uses, even though its dependency list requires those
#     packages unconditionally. So the wheel is installed with --no-deps and
#     the genuinely needed dependencies are listed explicitly below.
#   * lmcache_native.so links libc10.so and libtorch_cpu.so and nothing else.
#     A CPU-only torch satisfies it -- provided the version matches the one the
#     wheel was built against, since that linkage is ABI-sensitive.
#
# `make up` builds this on demand, so there is normally nothing to run by hand.
# To build it directly, from the repo root:
#   docker build -f docker/Dockerfile -t lmcache-fleet-demo/lmcache:0.5.8-cpu .

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG LMCACHE_VERSION=0.5.8
# Must match the torch that lmcache_native was compiled against; a mismatch
# shows up as an undefined-symbol ImportError on `import lmcache`.
ARG TORCH_VERSION=2.14.0

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

# A venv at /opt/venv keeps the binary paths identical to the upstream image,
# so the same compose commands and healthchecks work against either one.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

# CPU torch, from PyTorch's CPU index rather than PyPI, whose default Linux
# wheel is the GPU build and drags in ~3 GB of accelerator runtime packages.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}"

# lmcache itself, without its dependency closure.
RUN pip install --no-deps "lmcache==${LMCACHE_VERSION}"

# lmcache's real runtime dependencies, minus the GPU-only ones it requires
# unconditionally (cupy-cuda13x, cuda-python, cufile-python, nvtx).
# Optional-backend packages (awscrt, redis, google-cloud-bigtable) are kept
# because the L2 adapter factory imports them lazily by name, and a missing one
# turns into a confusing runtime error rather than a clean skip. pytest is here
# because the plugin suites need both it and lmcache importable in the same
# interpreter -- that is what `make test-plugins` runs.
RUN pip install \
        aiofile \
        aiofiles \
        aiohttp \
        awscrt \
        blake3 \
        cachetools \
        cryptography \
        fastapi \
        google-api-core \
        google-cloud-bigtable \
        "grpcio>=1.78.0" \
        "grpcio-observability==1.78.0" \
        httptools \
        httpx \
        "huggingface_hub>=1.5.0" \
        msgspec \
        numba \
        "numpy<=2.2.6" \
        "opentelemetry-api<=1.40.0,>=1.20.0" \
        "opentelemetry-exporter-otlp>=1.20.0" \
        "opentelemetry-exporter-prometheus<=0.61b0,>=0.50b0" \
        "opentelemetry-sdk>=1.20.0" \
        "prometheus_client<=0.24.1,>=0.18.0" \
        "protobuf<7,>=6.31.1" \
        psutil \
        py-cpuinfo \
        "pytest>=7" \
        pyyaml \
        "pyzmq>=25.0.0" \
        redis \
        safetensors \
        "setuptools<81.0.0,>=77.0.3" \
        "setuptools_scm>=8" \
        sortedcontainers \
        "transformers>=5.4" \
        uvicorn

# Fail the build rather than ship an image whose native extension cannot load.
RUN python -c "import torch, lmcache, pytest; \
from lmcache.lmcache_native import Bitmap; \
print('torch', torch.__version__, '| accelerator', torch.cuda.is_available())" \
    && lmcache --help > /dev/null

# No ENTRYPOINT on purpose: the point of this image is that nothing runs before
# the command. Compose passes the full /opt/venv/bin/lmcache ... invocation.
CMD ["lmcache", "--help"]
