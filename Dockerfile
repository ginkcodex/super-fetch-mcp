# super-fetch MCP server over stdio, with the browser, Xvfb and fonts included.
#
#   docker build -t super-fetch .
#   docker run -i --rm --shm-size 1g -v super-fetch-data:/data super-fetch

FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH

# Firefox's system libraries, plus Xvfb (a virtual display, so the browser needn't run headless) and
# fonts. Keep the Playwright version in step with uv.lock.
RUN uvx playwright@1.62.0 install-deps firefox && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app && mkdir /app /data && chown app /app /data
USER app
WORKDIR /app

# Dependencies first, so code changes don't redo the browser download below.
COPY --chown=app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# The browser and its fingerprint model; this must run as the user that owns the venv. The image
# always presents as Windows (the most common desktop OS), so the fonts that only macOS or Linux
# fingerprints use (~1 GB) are deleted in the same layer.
ENV SUPER_FETCH_BROWSER_OS=windows
RUN python -m camoufox fetch && python -c "\
import json, shutil; from pathlib import Path; from camoufox.pkgman import camoufox_path; \
fonts = Path(camoufox_path()) / 'fonts'; \
keep = set(json.loads((fonts / 'groups.json').read_text())['readBy']['win']); \
[shutil.rmtree(d) for d in fonts.iterdir() if d.is_dir() and d.name not in keep]"

COPY --chown=app README.md ./
COPY --chown=app src ./src
RUN uv sync --frozen --no-dev

ENV SUPER_FETCH_DATA_DIR=/data
VOLUME /data
# For `serve --http` (docker-compose.yml); agents launching it over stdio ignore this.
EXPOSE 8000
ENTRYPOINT ["super-fetch"]
