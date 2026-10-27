FROM --platform=$TARGETPLATFORM devlikeapro/waha:gows-2026.9.1

ARG BUILD_VERSION=0.2.4

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app"

COPY run.mjs /ha/run.mjs
COPY control.mjs /ha/control.mjs
COPY discovery.mjs /ha/discovery.mjs
COPY control.html /ha/control.html

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8099/health || exit 1

CMD ["node", "/ha/run.mjs"]
