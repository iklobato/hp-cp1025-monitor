# The image has no config: mount the Pi's carrossel.toml at /config/carrossel.toml.
FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-dejavu-extra \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pillow==11.*"

RUN useradd --create-home --uid 1000 dashboard
USER dashboard
WORKDIR /app
COPY --chown=dashboard:dashboard . /app

ENV PI_DASHBOARD_CONFIG=/config/carrossel.toml \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2
ENTRYPOINT ["python3", "/app/screen.py"]
CMD ["run"]
