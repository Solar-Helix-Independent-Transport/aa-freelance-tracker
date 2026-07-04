"""App Settings"""

# Django
from django.conf import settings

# Default/max window (in days) for the "last N days" leaderboard
LEADERBOARD_DEFAULT_DAYS = getattr(settings, "FREELANCE_TRACKER_LEADERBOARD_DEFAULT_DAYS", 30)
LEADERBOARD_MAX_DAYS = getattr(settings, "FREELANCE_TRACKER_LEADERBOARD_MAX_DAYS", 365)

# Number of rows shown per career leaderboard
LEADERBOARD_SIZE = getattr(settings, "FREELANCE_TRACKER_LEADERBOARD_SIZE", 15)
