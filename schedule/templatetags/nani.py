from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def hm(minutes):
    """420 -> '7:00'"""
    if minutes is None:
        return ""
    minutes = int(minutes)
    sign = "−" if minutes < 0 else ""
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h}:{m:02d}"


@register.filter
def hm_signed(minutes):
    """90 -> '+1:30', -30 -> '−0:30'"""
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
