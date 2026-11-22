"""OpenRouter sort preferences available to Lantern's private chat worker."""

import re

ROUTES = {"fastest": "throughput", "cheapest": "price", "default": None}

ROUTE_TOOL = {
    "name": "set_provider_route",
    "description": (
        "Choose the OpenRouter provider preference for your next model request, including "
        "the next request in this turn. Fastest prefers higher token throughput, cheapest "
        "prefers lower prices, and default lets OpenRouter balance price and performance. "
        "The choice persists across turns, heartbeats and worker restarts until you change it. "
        "All preferences use the same model and daily budget."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["route"],
        "properties": {"route": {"type": "string", "enum": list(ROUTES)}},
    },
}


def validate_route(route):
    if not isinstance(route, str) or route not in ROUTES:
        raise ValueError("Choose default, fastest or cheapest for the OpenRouter provider preference.")


def safe_provider(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()/+-]{0,79}", value):
        return value
