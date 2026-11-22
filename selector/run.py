"""Entry point: import the selector as a module so its precompiled bytecode is used."""
import os
import sys

# Checks are forked from the selector and must hash strings the way the
# pristine fingerprints were computed (PYTHONHASHSEED=0): re-execute once
# with it (other PYTHON* variables dropped, as -I would).
if os.environ.get("PYTHONHASHSEED") != "0" and os.environ.get("BLAST_REEXEC") != "1":
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONHASHSEED"] = "0"
    env["BLAST_REEXEC"] = "1"
    try:
        os.execve(sys.executable, [sys.executable, "-s", "-P", os.path.abspath(__file__)], env)
    except OSError:
        pass  # keep going in this process; checks then run as subprocesses

sys.path.insert(0, "/app")
import blast  # noqa: E402

sys.exit(blast.main())
