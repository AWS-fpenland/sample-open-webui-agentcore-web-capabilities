# syntax=docker/dockerfile:1.7
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# AI-Q on Amazon Bedrock AgentCore — runtime container (linux/arm64).
# Built remotely by the run's CodeBuild project; never requires local Docker.
#
# Provenance: the upstream NVIDIA AI-Q Blueprint (Apache-2.0) is cloned at the
# exact commit in upstream.pin and installed from ITS OWN uv.lock (exported to a
# requirements file), so every dependency version equals upstream's lock with one
# documented deviation: `nemo-relay` is taken as the PyPI abi3 aarch64 wheel of
# the same minor (0.8.x) instead of the lock's git source build (avoids a Rust
# toolchain in the image build). Upstream source is not modified.
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS base
ARG SOURCE_COMMIT=unknown
ARG AIQ_UPSTREAM_REPO=https://github.com/NVIDIA-AI-Blueprints/aiq.git
ARG AIQ_UPSTREAM_REF=bf4e67d1564ef8d2ec8f65b5f9001e512befc095
ARG NEMO_RELAY_PIN=nemo-relay==0.8.4
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1 \
    AIQ_SOURCE_COMMIT=${SOURCE_COMMIT} AIQ_UPSTREAM_REF=${AIQ_UPSTREAM_REF} AIQ_UPSTREAM_REPO=${AIQ_UPSTREAM_REPO}
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl g++ \
    && rm -rf /var/lib/apt/lists/*

# 1. Upstream AI-Q at the pinned commit (source kept: workspace packages are installed editable)
RUN git clone --quiet --filter=blob:none ${AIQ_UPSTREAM_REPO} /opt/aiq \
    && git -C /opt/aiq checkout --quiet ${AIQ_UPSTREAM_REF} \
    && git -C /opt/aiq log -1 --format='upstream %H %ci %s' \
    && rm -rf /opt/aiq/.git /opt/aiq/frontends/ui /opt/aiq/docs

# 2. Dependencies exactly as upstream's lock (root package + knowledge_layer), nemo-relay from PyPI wheel
WORKDIR /opt/aiq
RUN uv export --frozen --no-dev --no-hashes --no-emit-project --package aiq-agent -o /tmp/req.txt \
    && sed -i -E "s|^nemo-relay @ git\+.*$|${NEMO_RELAY_PIN}|" /tmp/req.txt \
    && grep -n '^nemo-relay' /tmp/req.txt \
    && uv venv /opt/venv --python 3.12 \
    && uv pip install --python /opt/venv/bin/python -r /tmp/req.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps -e /opt/aiq \
    && rm -rf /root/.cache/uv
ENV PATH="/opt/venv/bin:${PATH}" VIRTUAL_ENV=/opt/venv

# 3. This adapter (AWS-side): identity, jobs/events, AgentCore-native tools, entrypoint
WORKDIR /app
COPY pyproject.toml README.md upstream.pin ./
COPY src ./src
COPY configs ./configs
# Adapter dependencies not present in upstream's lock (bedrock-agentcore SDK, PyJWT). uv keeps every
# already-installed locked version that satisfies the constraints; the printed report shows any change.
RUN uv pip freeze --python /opt/venv/bin/python > /tmp/before.txt \
    && uv pip install --python /opt/venv/bin/python "bedrock-agentcore>=1.23.0,<2" "PyJWT[crypto]>=2.9,<3" "playwright>=1.50,<2" \
    && uv pip install --python /opt/venv/bin/python --no-deps -e . \
    && uv pip freeze --python /opt/venv/bin/python > /tmp/after.txt \
    && echo "== dependency deviations from upstream lock (only lines that changed) ==" \
    && (diff <(grep -v '^-e\|^aiq' /tmp/before.txt) <(grep -v '^-e\|^aiq' /tmp/after.txt) || true) \
    && python -c "import aiq_agentcore, aiq_agentcore.app, aiq_agentcore.engine_aiq, aiq_agentcore.nat_plugins.web_search, aiq_agentcore.nat_plugins.knowledge, aiq_agent, nat, langchain_aws, bedrock_agentcore, sys; print('aiq_agentcore', aiq_agentcore.__version__, 'python', sys.version.split()[0])" \
    && python -c "import importlib.metadata as m; print({p: m.version(p) for p in ['nvidia-nat-core','langchain-aws','nemo-relay','deepagents','bedrock-agentcore','boto3','pydantic','uvicorn','starlette']})"

# Non-root; AgentCore contract: HTTP :8080 with POST /invocations and GET /ping
RUN useradd --create-home --uid 10001 aiq && mkdir -p /app/state && chown -R aiq:aiq /app /opt/aiq
USER aiq
EXPOSE 8080
ENV AIQ_CONFIG=/app/configs/aiq_bedrock.yml AIQ_CHECKPOINT_DB=/app/state/checkpoints.db HOME=/home/aiq
CMD ["python", "-m", "aiq_agentcore.app"]
