"""Configuration constants."""

SETTINGS = {
    "ddd": True,
    "mode": "ddd mode",
}


def describe():
    return "ddd is enabled" if SETTINGS["ddd"] else "ddd is disabled"
