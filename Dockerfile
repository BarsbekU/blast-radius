# Blast Radius submission. The selector needs nothing beyond the standard
# library: the maps in selector/maps were built ahead of time from sqlglot at
# eb11aec (see tools/mapbuild/), and src.zip holds that commit's files so
# patches can be applied and compared at run time.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY selector/ /app/

# A pristine, precompiled copy of the sqlglot package, and the fingerprints of
# its class attributes and module data. For a definition-time change (a table
# entry, a class constant...) the selector imports the patched package in a
# subprocess and compares fingerprints with these to see what really changed.
RUN python -c "import zipfile; z = zipfile.ZipFile('/app/maps/src.zip'); [z.extract(n, '/app/pristine') for n in z.namelist() if n.startswith('sqlglot/')]" \
    && python -m compileall -q /app/pristine > /dev/null \
    && python -c "import compileall; compileall.compile_file('/app/blast.py', quiet=1)" \
    && echo '{"root": "/app/pristine"}' \
       | PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python -P -s /app/fingerprint.py 2>/dev/null \
       | python -c "import gzip, json, sys; d = json.load(sys.stdin); assert d['ok'] and not d['failed'], d.get('error'); gzip.open('/app/fp_baseline.json.gz', 'wt').write(json.dumps(d))" \
    && chmod -R a+rX /app

CMD ["python", "-I", "/app/run.py"]
