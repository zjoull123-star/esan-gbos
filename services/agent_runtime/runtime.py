"""Safe default ASGI entrypoint for the local Agent read service.

Runtime dependencies and credentials must be injected by the local launcher.
Importing this module never opens a database connection or enables a writer.
"""

from .api import create_agent_runtime_app

app = create_agent_runtime_app()

__all__ = ["app"]
