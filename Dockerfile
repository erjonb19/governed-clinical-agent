# Dockerfile -- the API as a container image (backlog item 17).
#
#   docker build -t governed-clinical-agent .
#   docker run -p 8000:8000 -e API_KEY=change-me -e GEMINI_API_KEY=... governed-clinical-agent
#
# Slim base, pinned requirements (requirements-api.txt, the same file Render
# installs), a non-root user, and a HEALTHCHECK on /health. The hospital Gold is
# committed and baked in; the FHIR Gold is derived and gitignored, so mount it
# at /app/medallion/fhir_gold.duckdb if you have it (see docker-compose.yml).
# Secrets come from the environment at run time, never from the image.

# pinned by digest (audit item 50); Dependabot's docker updates move it
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# dependencies first, so a code change does not reinstall them
COPY requirements-api.lock /app/requirements-api.lock
RUN pip install --require-hashes -r /app/requirements-api.lock

# a non-root user owns only what it must write: logs (traces, audit,
# checkpoints, approvals live under logs/ by default)
RUN useradd --create-home --uid 10001 app
COPY . /app
RUN mkdir -p /app/logs && chown -R app:app /app/logs
USER app

# /health reports this (versioning.git_commit); CI passes the commit sha. Last,
# so a new commit does not invalidate the dependency layers.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

EXPOSE 8000
# python, not curl: the slim image has no curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
