# Hidden Hand arena. Each fight runs in its own throwaway container of this image
# (match_runner.py), as an unprivileged user, with no network and a read-only root.
FROM python:3.12-slim
# python:3.12-slim already ships zoneinfo, so TZ is honoured with no extra packages.
RUN useradd --create-home arena && mkdir /data && chown arena:arena /data
# Docker CLI only (no daemon): the arena starts one container per fight on the
# HOST's daemon through the socket docker-compose.yml mounts. Fight containers
# use this same image and get no socket.
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY --chown=root:root . .
USER arena
ENV ARENA_DATA=/data PORT=8000 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
VOLUME /data
# stdlib-only probe: python:3.12-slim ships no curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/')"
CMD ["python", "arena.py"]
