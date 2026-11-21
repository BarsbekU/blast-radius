# Blast Radius submission. The selector needs nothing beyond the standard
# library: the maps in selector/maps were built ahead of time from sqlglot at
# eb11aec (see tools/mapbuild/), and src.zip holds that commit's files so
# patches can be applied and compared at run time.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY selector/ /app/
# The scorer runs the container as nobody on a read-only disk.
RUN python -c "import compileall; compileall.compile_file('/app/blast.py', quiet=1)" \
    && chmod -R a+rX /app

CMD ["python", "-I", "/app/run.py"]
