"""Entry point for the PyCharm FastAPI run configuration (``uvicorn main:app``).

The application itself lives in :mod:`ledger.api.app`.
"""

from ledger.api.app import create_app

app = create_app()
