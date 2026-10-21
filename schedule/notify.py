"""
Emails the nanny when the parents change her plan (idea taken from the chef app, which
emails the family when the menu changes).

Usage: take a snapshot of the affected days before a change and pass it to
`plan_changed` afterwards; only days from today on that actually look different are
mentioned, and nothing is sent when nothing changed.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import engine
from .templatetags.nani import timespan

logger = logging.getLogger(__name__)


def site_url(request=None):
    """Where the app is reached: SITE_URL, else the address of the current request."""
    if settings.SITE_URL:
        return settings.SITE_URL.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/") if request else ""


def describe(info):
    """How a day looks for the nanny, e.g. '15:00–19:00' or 'no care needed'."""
    if info.contract is None:
        return None
    if info.status == engine.Status.HOLIDAY:
        text = f"holiday ({info.holiday.name})"
        return f"{text}, {timespan(info.planned)}" if info.planned else text
    if info.status == engine.Status.WORK and info.planned:
        return timespan(info.planned)
    return {
        engine.Status.NOT_NEEDED: "no care needed",
        engine.Status.VACATION: "vacation",
        engine.Status.SICK: "sick",
    }.get(info.status, "day off")


def snapshot(start, end):
    """{date: description} for the days from today on within start..end."""
    start = max(start, timezone.localdate())
    if end < start:
        return {}
    return {d.date: describe(d) for d in engine.compute_days(start, end)}


def plan_changed(request, before):
    """Compares the days in `before` with how they look now and emails the nanny the differences."""
    if not before or not settings.PLAN_EMAILS or not request.user.is_parent:
        return
    after = snapshot(min(before), max(before))
    changes = [(day, after.get(day), was) for day, was in sorted(before.items()) if after.get(day) != was]
    if not changes:
        return

    lines = [f"  {day:%a %d %b}: {now or 'no contract'} (was {was or 'no contract'})" for day, now, was in changes]
    first = changes[0][0]
    link = site_url(request) + reverse("schedule:week_of", args=[first.isoformat()])
    for nanny in User.objects.filter(role=User.Role.NANNY, is_active=True).exclude(email=""):
        body = (
            f"Hi {nanny.get_short_name()},\n\n"
            f"{request.user.get_short_name()} updated the plan:\n\n"
            + "\n".join(lines)
            + f"\n\nSee it in {settings.SITE_NAME}:\n{link}\n"
        )
        try:
            send_mail(f"[{settings.SITE_NAME}] Plan updated", body, settings.DEFAULT_FROM_EMAIL, [nanny.email])
        except Exception:  # A notification must never break the request.
            logger.exception("Could not email plan changes to %s", nanny.email)
