"""Explicit server-management coverage; a capability listing never dispatches commands."""

import json
from importlib.resources import files


def list_management_capabilities():
    payload = json.loads(files("stackorion").joinpath("management_capabilities.json").read_text())
    return {**payload, "coverage": "catalog of implemented, disconnected and missing operations; execution requires its registered MCP tool"}
