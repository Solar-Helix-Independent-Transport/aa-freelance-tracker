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


def with_optional_token(operation, token):
    """Attach a token to an optionally-authenticated ESI operation.

    GetFreelanceJobsDetail has no `security` requirement in the ESI spec -
    public jobs need no auth - but ACL-protected jobs require a token from
    a participant or the corp's freelance job manager to view at all.
    django-esi's client rejects a token passed via the `token=` kwarg on any
    operation without a declared security requirement, so it's attached
    directly to the operation instead, which bypasses that guard while
    still sending the Authorization header.
    """
    operation.token = token
    return operation
