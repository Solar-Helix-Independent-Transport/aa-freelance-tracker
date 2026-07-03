"""Hook into Alliance Auth"""

# Django
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

# AA Freelance Tracker
from freelance_tracker import urls


class FreelanceTrackerMenuItem(MenuItemHook):
    """This class ensures only authorized users will see the menu entry"""

    def __init__(self):
        # setup menu entry for sidebar
        MenuItemHook.__init__(
            self,
            _("Freelance Jobs"),
            "fas fa-briefcase fa-fw",
            "freelance_tracker:index",
            navactive=["freelance_tracker:"],
        )

    def render(self, request):
        """Render the menu item"""

        if request.user.has_perm("freelance_tracker.basic_access"):
            return MenuItemHook.render(self, request)

        return ""


@hooks.register("menu_item_hook")
def register_menu():
    """Register the menu item"""

    return FreelanceTrackerMenuItem()


@hooks.register("url_hook")
def register_urls():
    """Register app urls"""

    return UrlHook(urls, "freelance_tracker", r"^freelance-tracker/")
