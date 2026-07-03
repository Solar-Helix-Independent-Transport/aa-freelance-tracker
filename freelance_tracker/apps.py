"""App Configuration"""

# Django
from django.apps import AppConfig

# AA Freelance Tracker
from freelance_tracker import __version__


class FreelanceTrackerConfig(AppConfig):
    """App Config"""

    name = "freelance_tracker"
    label = "freelance_tracker"
    verbose_name = f"Freelance Tracker v{__version__}"
