# clipforge image: linux/amd64 (runs on the operator's Intel Mac via Docker Desktop). One image, two services.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# ffmpeg (Debian build includes libx264 and libass), fonts, tz database for America/New_York.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libegl1 libgles2 fonts-dejavu-core tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

WORKDIR /app

# Dependencies first (cached layer), then the project itself, from the committed lockfile only.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY scripts ./scripts
# Runtime data the package loads from <cwd> (WORKDIR /app): caption fonts + face model (render) and prompts (rank).
COPY assets ./assets
COPY prompts ./prompts
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

ENV PATH=/opt/venv/bin:$PATH \
    CLIPFORGE_CONFIG=/app/config/clipforge.yaml \
    CLIPFORGE_DATA_DIR=/app/data \
    CLIPFORGE_INBOX_DIR=/app/inbox

# Fail the build if a native dependency cannot load on this base image.
RUN python -c "import cv2, scenedetect, av, faster_whisper, mediapipe; from mediapipe.tasks.python import vision; \
from zoneinfo import ZoneInfo; ZoneInfo('America/New_York'); print('imports ok, cv2', cv2.__version__)" \
    && ffmpeg -hide_banner -filters | grep -q ' ass ' \
    && ffmpeg -hide_banner -encoders | grep -q libx264 \
    && python -c "from clipforge.render.assets import missing_fonts, face_model_path; \
assert not missing_fonts(['Montserrat-ExtraBold', 'Inter-Bold', 'Inter-Black']), 'fonts missing'; \
assert face_model_path().is_file(), 'face model missing'; \
from clipforge.rank.prompts import load_prompt; load_prompt('score_windows'); load_prompt('clip_copy'); \
print('assets and prompts ok')"

RUN useradd --create-home --uid 1000 clipforge \
    && mkdir -p /app/data /app/inbox /app/config \
    && chown -R clipforge /app/data /app/inbox
USER clipforge

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD python -m clipforge.heartbeat "${CLIPFORGE_SERVICE:-worker}"

CMD ["cf", "--help"]
