from django.conf import settings


def nav(request):
    user = getattr(request, "user", None)
    is_parent = bool(user and user.is_authenticated and user.is_parent)
    context = {"site_name": settings.SITE_NAME, "is_parent": is_parent}
    if is_parent:
        from .models import Vacation

        context["pending_count"] = Vacation.objects.filter(status=Vacation.Status.PENDING).count()
    return context
