# wheel in the builder, runtime image stays without a compiler
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY slo_watch ./slo_watch
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels slo-watch \
 && rm -rf /wheels \
 && useradd --user-group --uid 10001 --create-home slo-watch \
 && mkdir -p /data && chown slo-watch:slo-watch /data
COPY config.yml /app/config.yml
# db goes on the volume, everything else in the image is read-only
ENV SLO_WATCH_DB=/data/slo-watch.db
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
ENTRYPOINT ["slo-watch"]
CMD ["-c", "/app/config.yml", "serve", "--host", "0.0.0.0", "--port", "8080"]
