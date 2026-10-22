from decimal import Decimal

from django import template

register = template.Library()

TRIP_SUGGESTIONS = 30


@register.inclusion_tag("schedule/_trip_suggestions.html")
def trip_suggestions():
    """Earlier trip purposes for the suggestions below the purpose field: the most frequent first,
    each with the km of its latest trip (app.js shows them while typing and fills in the km)."""
    from schedule.models import Trip

    seen = {}
    for trip in Trip.objects.order_by("-date", "-id").only("purpose", "km")[:1000]:
        key = trip.purpose.strip().lower()
        if key in seen:
            seen[key]["count"] += 1
        else:
            seen[key] = {"purpose": trip.purpose.strip(), "km": trip.km, "count": 1}
    ranked = sorted(seen.values(), key=lambda s: -s["count"])  # stable: equally frequent stay most recent first
    return {"suggestions": ranked[:TRIP_SUGGESTIONS]}


@register.filter
def hm(minutes):
    """A duration: 420 -> '7h', 90 -> '1h30m', 5 -> '0h05m'. (Clock times use timespan.)"""
    if minutes is None:
        return ""
    minutes = int(minutes)
    sign = "−" if minutes < 0 else ""
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h}h{m:02d}m" if m else f"{sign}{h}h"


@register.filter
def hm_signed(minutes):
    """90 -> '+1h30m', -30 -> '−0h30m', 60 -> '+1h'"""
    minutes = int(minutes or 0)
    return hm(minutes) if minutes < 0 else f"+{hm(minutes)}"


@register.filter
def sign_class(minutes):
    minutes = int(minutes or 0)
    return "pos" if minutes > 0 else "neg" if minutes < 0 else "zero"


@register.filter
def eur(value):
    """1234.5 -> '1.234,50 €'"""
    if value is None:
        return ""
    text = f"{Decimal(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


@register.filter
def timespan(pair):
    """(time, time) -> '15:00–19:00'"""
    if not pair or not pair[0]:
        return ""
    return f"{pair[0]:%H:%M}–{pair[1]:%H:%M}"


@register.filter
def days(value):
    """Vacation days without trailing zeros: 6.25 -> '6.25', 25.00 -> '25'."""
    value = Decimal(value).normalize()
    return f"{value:f}"
