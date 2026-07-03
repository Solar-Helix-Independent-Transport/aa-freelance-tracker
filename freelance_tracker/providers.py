"""ESI Client Provider"""

# Alliance Auth (ESI)
# Alliance Auth
from esi.openapi_clients import ESIClientProvider

# AA Freelance Tracker
from freelance_tracker import __title__, __url__, __version__

esi = ESIClientProvider(
    compatibility_date="2026-06-09",
    ua_appname=__title__,
    ua_version=__version__,
    ua_url=__url__,
    tags=["Freelance Jobs"],
)
