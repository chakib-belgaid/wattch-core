FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-setuptools \
    && rm -rf /var/lib/apt/lists/*

COPY python/ /opt/wattch/python/
RUN python3 -m venv --system-site-packages /opt/wattch/venv \
    && /opt/wattch/venv/bin/pip --disable-pip-version-check install \
       --no-index --no-deps --no-build-isolation /opt/wattch/python \
    && useradd --uid 1000 --create-home --shell /bin/sh wattch

COPY bin/ /usr/local/bin/
COPY test_fixture.py /test_fixture.py
CMD ["python3", "/test_fixture.py"]
