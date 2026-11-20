ARG BASE=python:3.12-slim-bookworm
FROM ${BASE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git poppler-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && mkdir -p /work
COPY render_pdf.py /opt/lantern/render_pdf.py
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/bin/sh"]
