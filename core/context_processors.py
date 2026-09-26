from .access import can_create_endorsement
from .models import BOOTSWATCH_THEMES, UserProfile


def current_profile(request):
    context = {
        "current_profile": None,
        "bootswatch_themes": BOOTSWATCH_THEMES,
        "color_modes": UserProfile.ColorMode.choices,
        "unread_notifications": 0,
        "portal_can_create": False,
    }
    if request.user.is_authenticated:
        try:
            context["current_profile"] = request.user.profile
            context["unread_notifications"] = request.user.portal_notifications.filter(is_read=False).count()
            context["portal_can_create"] = can_create_endorsement(request.user)
        except Exception:
            pass
    return context
