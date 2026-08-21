"""Import-compatibility test against the REAL installed Home Assistant.

If the integration uses any ``homeassistant.*`` symbol that no longer exists in
the installed HA version, this fails at import time -- which is exactly the
"completely cannot add" failure mode a user hits (the integration won't even
load). This is the first line of defence and runs before any functional test.
"""
from __future__ import annotations


def test_import_integration_modules() -> None:
    """Every integration module must import cleanly against the real HA API."""
    import musicflow_cast  # noqa: F401
    from musicflow_cast import (  # noqa: F401
        api,
        browse_media,
        config_flow,
        const,
        coordinator,
        discovery,
        dlna,
        media_player,
    )

    assert musicflow_cast.DOMAIN == "musicflow_cast"
    # CONFIG_SCHEMA is built at import time via cv.config_entry_only_config_schema
    assert musicflow_cast.CONFIG_SCHEMA is not None
