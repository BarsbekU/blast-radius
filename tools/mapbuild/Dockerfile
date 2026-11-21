# Map-building image: the labeling environment (same base image digest and
# requirements.lock) plus a tracer that every Python process loads at startup.
# Build from the repository root:
#   docker build -f tools/mapbuild/Dockerfile -t blast-mapbuild .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

COPY tools/mapbuild/blast_trace.py /usr/local/lib/python3.13/site-packages/blast_trace.py
RUN echo "import blast_trace" > /usr/local/lib/python3.13/site-packages/zz_blast_trace.pth

# sqlglot at the pinned commit (work/sqlglot is a checkout of eb11aec), the
# whole checkout as the labelers had it (tests read sqlglotc/setup.py etc.).
COPY work/sqlglot /src
# Precompile so each of the ~1600 single-test processes does not recompile
# sqlglot from source (PYTHONDONTWRITEBYTECODE below keeps the tree clean).
RUN python -m compileall -q /src/sqlglot /src/tests > /dev/null
COPY sample/tests.json /data/tests.json
COPY tools/mapbuild/*.py /tools/

ENV PYTHONPATH=/src SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
