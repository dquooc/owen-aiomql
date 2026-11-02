"""Module execution delegates to the same callable composition root."""

from .entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
