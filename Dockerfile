# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . .

RUN python -m pip install "setuptools==80.9.0" "Cython==3.0.12" \
    && python -m pip install --prefix=/runtime-python "toml>=0.10.2" "py_trees>=2.5,<2.6" \
    && python setup.py build_ext --inplace \
    && PYTHONPATH=/runtime-python/lib/python3.12/site-packages python run_server.py --version \
    && rm -rf build


FROM python:3.12-slim-bookworm AS runtime

ARG VCS_REF=""
ARG IMAGE_VERSION="dev"

LABEL org.opencontainers.image.title="BattleSpades" \
      org.opencontainers.image.description="Ace of Spades Battle Builder dedicated server" \
      org.opencontainers.image.source="https://github.com/KikoTs/BattleSpades" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BATTLESPADES_DATA_DIR=/data

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 battlespades \
    && useradd --uid 10001 --gid battlespades --no-create-home --home-dir /app battlespades \
    && install -d -o battlespades -g battlespades /app /data

COPY --from=builder /runtime-python/ /usr/local/
COPY --from=builder --chown=battlespades:battlespades /build/ /app/

WORKDIR /app
USER 10001:10001

VOLUME ["/data"]
EXPOSE 27015/udp
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-u", "scripts/container_entrypoint.py"]
