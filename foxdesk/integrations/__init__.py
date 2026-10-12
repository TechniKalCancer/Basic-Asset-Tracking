"""Connectors to outside systems (Google Workspace, KACE, vendor APIs).

The repo now treats vendor-specific device metadata as a separate but shared
concern: a district may mix Dell, Lenovo, and HP devices, and each integration
should normalize them behind a common vendor profile instead of hard-coding
brand-specific assumptions throughout the app.
"""

from foxdesk.integrations.vendors import (  # noqa: F401
    normalize_device_identity,
    normalize_vendor_name,
    supported_vendors,
    vendor_from_serial,
)
