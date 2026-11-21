# Blast Radius submission.
#
# selector/maps holds what was measured ahead of time on sqlglot at eb11aec
# (see tools/mapbuild/): per-test line coverage, file reads, class attribute
# reads, plus the pinned checkout itself (src.zip).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY selector/ /app/

# /app/tree: the pinned sqlglot package, precompiled. fp_baseline: the
# fingerprints of its class attributes and module data; for an import-time
# change the selector imports the patched package in a subprocess and
# compares fingerprints to see exactly which values changed.
RUN python -c "import zipfile; z = zipfile.ZipFile('/app/maps/src.zip'); [z.extract(n, '/app/tree') for n in z.namelist() if n.startswith('sqlglot/')]" \
    && python -m compileall -q /app/tree/sqlglot > /dev/null \
    && python -c "import compileall; compileall.compile_file('/app/blast.py', quiet=1)" \
    && echo '{"root": "/app/tree"}' \
       | PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python -P -s /app/fingerprint.py 2>/dev/null \
       | python -c "import gzip, json, sys; d = json.load(sys.stdin); assert d['ok'] and not d['failed'], d.get('error'); gzip.open('/app/fp_baseline.json.gz', 'wt').write(json.dumps(d))" \
    && chmod -R a+rX /app

ENV BLAST_PRISTINE=/app/tree
CMD ["python", "-I", "/app/run.py"]
