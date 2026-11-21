"""Entry point: import the selector as a module so its precompiled bytecode is used."""
import sys

sys.path.insert(0, "/app")
import blast  # noqa: E402

sys.exit(blast.main())
