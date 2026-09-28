# cloudprice-mcp, HTTP transport, for Kubernetes.
#
# TARGET IS ARM64. The AKS node this runs on is Standard_B2pls_v2, which is
# aarch64 (the "p"). An amd64 image pushes fine, the Deployment is accepted, and
# the pod then sits Pending or CrashLoops with a message about node affinity
# that never once says "architecture". Build it with:
#
#   docker buildx build --platform linux/arm64 ...
#
# or on a native ARM runner, which is what the workflow does.
#
# TWO THINGS THAT BREAK THIS IMAGE IF YOU CHANGE THEM CARELESSLY
#
# 1. --no-deps on the install. The package declares mcp>=1.0.0,<2 for the stdio
#    server, and dispatch does not import it. Installing normally would pull the
#    whole MCP SDK into an image that can never use it - more bytes and more CVE
#    surface for nothing. Runtime deps are therefore listed explicitly below,
#    and if dispatch ever grows a new import this Dockerfile must gain it too.
#
# 2. The package DATA. src/cloudprice_mcp/data is ~1.4 MB of price catalogue and
#    it is the whole point of the product. `pip install .` carries it because
#    the package is installed properly rather than copied loose - which is also
#    why /readyz loads the catalogue: an image that lost the data starts up
#    perfectly and fails every single request.

# ---- build ----------------------------------------------------------------
# Base image pinned BY DIGEST, not by the 3.12-slim tag.
# A tag is mutable: the same Dockerfile can build different code on
# different days, which makes a build unreproducible and lets an upstream
# compromise reach this image without any change here. Update the digest
# deliberately, as a reviewable commit.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build

WORKDIR /build

# Dependency layer first so a source-only change does not re-resolve pip.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir \
      "httpx>=0.27.0" \
      "pyyaml>=6.0" \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.30"

COPY pyproject.toml README.md ./
COPY src/ ./src/

# --no-deps: see note 1 above.
RUN pip install --no-cache-dir --no-deps .

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Non-root. Kubernetes will not enforce this for you unless a securityContext
# says so, and the Helm chart does - but an image that only works as root makes
# that setting fail at deploy time rather than never being needed.
RUN useradd --create-home --uid 10001 app

COPY --from=build /opt/venv /opt/venv
COPY http_api/app.py /app/app.py

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
USER app
EXPOSE 8080

# No HEALTHCHECK instruction on purpose: Kubernetes probes this through
# /healthz and /readyz, and a Docker-level healthcheck would be a second,
# invisible source of truth that the cluster ignores.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
