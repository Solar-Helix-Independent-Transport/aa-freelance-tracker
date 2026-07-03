"""Template filters mapping job data to Bootstrap contextual color keywords.

These only decide *which* Bootstrap class to use (badge/border/text color) -
the actual styling stays entirely in Bootstrap, so templates render correctly
under any Bootstrap theme rather than hardcoded colors.
"""

# Django
from django import template

# AA Freelance Tracker
from freelance_tracker.models import FreelanceJob

register = template.Library()

_CAREER_COLORS = {
    FreelanceJob.Career.EXPLORER: "info",
    FreelanceJob.Career.INDUSTRIALIST: "warning",
    FreelanceJob.Career.ENFORCER: "danger",
    FreelanceJob.Career.SOLDIER_OF_FORTUNE: "primary",
}

_STATE_BADGE_COLORS = {
    FreelanceJob.State.ACTIVE: "success",
    FreelanceJob.State.CLOSED: "secondary",
    FreelanceJob.State.COMPLETED: "primary",
    FreelanceJob.State.EXPIRED: "warning",
    FreelanceJob.State.DELETED: "danger",
    FreelanceJob.State.UNSPECIFIED: "secondary",
}


@register.filter
def career_color(career: str) -> str:
    """Bootstrap contextual color keyword for a job's career"""

    return _CAREER_COLORS.get(career, "secondary")


@register.filter
def state_badge_color(state: str) -> str:
    """Bootstrap contextual color keyword for a job's state badge"""

    return _STATE_BADGE_COLORS.get(state, "secondary")
