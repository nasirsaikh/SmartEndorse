def current_profile(request):
    if request.user.is_authenticated:
        try:
            return {"current_profile": request.user.profile}
        except Exception:
            pass
    return {"current_profile": None}
