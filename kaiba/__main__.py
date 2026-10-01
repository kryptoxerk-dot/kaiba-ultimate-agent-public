"""Allow `python -m kaiba` to use the installed CLI."""

from kaiba.cli.main import app

if __name__ == "__main__":
    app()
