# RestaurantOS AI image (ARCHITECTURE §3): a node stage builds the SPA, python:3.12-slim serves it with the API.
#
# The build context is the allowlist in .dockerignore and every COPY below names an allowlisted path, so no credential
# file, data/ or docs/planning/ can reach a layer (tests/infra/test_dockerignore.py). Fly builds it remotely
# (`fly deploy --remote-only`), so the founders' Intel Mac needs no Docker. No torch and no ffmpeg.
#
# The runtime keeps the repo layout, so config.REPO_ROOT resolves to /app:
#   /app/src/restaurantos  the package, on sys.path through a .pth file in the venv (also inside `fly ssh` sessions)
#   /app/config            tenants, scenarios, lexicons, features.yaml
#   /app/src/web/dist      the built SPA
#   /data                  the ros_data volume: SQLite, ledgers, caches, backups, logs
#
# The container starts as root only to hand /data to the unprivileged `restos` user (Fly mounts volumes root-owned and
# `fly ssh` restores write as root), then drops privileges with setpriv. RESTOS_MAINTENANCE=1, set for a single deploy
# by scripts/deploy.sh init and scripts/restore.sh, keeps the machine up without the app so /data can be prepared.

ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim

# ---- 1. web: src/web -> dist --------------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /build/src/web
COPY src/web/package.json src/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/web/ ./
RUN npm run build

# ---- 2. deps: the runtime dependencies from pyproject.toml, in /opt/venv -------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /build
# An optional requirements.lock (`pip freeze --exclude-editable` of a known-good venv) pins versions as constraints.
COPY pyproject.toml requirements.lock* ./
RUN python -m venv /opt/venv \
 && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
      > requirements.txt \
 && if [ -f requirements.lock ]; then set -- -c requirements.lock; fi \
 && /opt/venv/bin/pip install "$@" -r requirements.txt

# ---- 3. runtime ---------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    RESTOS_HOST=0.0.0.0 \
    RESTOS_PORT=8000 \
    RESTOS_DATA_DIR=/data \
    RESTOS_WEB_DIST_DIR=/app/src/web/dist \
    RESTOS_FEATURES_PATH=/app/config/features.yaml \
    RESTOS_KEY_FILES=false
RUN groupadd --system --gid 10001 restos \
 && useradd --system --uid 10001 --gid restos --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin restos \
 && mkdir -p /data \
 && setpriv --version > /dev/null
COPY --from=deps /opt/venv /opt/venv
RUN echo /app/src > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/restaurantos-src.pth"
WORKDIR /app
COPY pyproject.toml ./
COPY config/ config/
COPY src/restaurantos/ src/restaurantos/
# The operator scripts that run on the machine: the phrase pre-render (P15b, a release step in scripts/deploy.sh)
# and the order-history simulator (pass 10). Until a script exists its wildcard matches nothing, which BuildKit
# (Fly's builders) copies as nothing instead of failing.
COPY scripts/build_phrase_cache.py* scripts/simulate_history.py* scripts/
COPY --from=web /build/src/web/dist/ src/web/dist/
RUN python -m compileall -q src/restaurantos \
 && python -c "import restaurantos.app, restaurantos.__main__"

# Last, so a new commit only rebuilds these layers. /api/health reports it as git_sha.
ARG RESTOS_GIT_SHA=""
ENV RESTOS_GIT_SHA=${RESTOS_GIT_SHA}
LABEL org.opencontainers.image.source="https://github.com/vamsikrishna2421/restaurantos-ai" \
      org.opencontainers.image.revision="${RESTOS_GIT_SHA}"

EXPOSE 8000
CMD if [ "${RESTOS_MAINTENANCE:-0}" = 1 ]; then \
      echo "RESTOS_MAINTENANCE=1: the app is not started; prepare /data over fly ssh, then deploy without it"; \
      exec sleep infinity; \
    fi; \
    if [ "$(id -u)" != 0 ]; then exec python -m restaurantos; fi; \
    mkdir -p "$RESTOS_DATA_DIR" && chown -R restos:restos "$RESTOS_DATA_DIR" \
    && exec setpriv --reuid=restos --regid=restos --init-groups python -m restaurantos
