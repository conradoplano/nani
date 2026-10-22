import logging
from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User

from . import engine, notify
from .forms import (
    AdjustmentForm, BulkPlanForm, ContractForm, DayForm, ExtraForm, HolidayForm, PersonForm, TripForm, VacationForm,
)
from .models import Adjustment, Contract, Day, Extra, Holiday, Trip, Vacation

logger = logging.getLogger(__name__)


def parent_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_parent:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Http404("Invalid date")


def _month_bounds(year, month):
    if not 1 <= month <= 12:
        raise Http404("Invalid month")
    first = date(year, month, 1)
    prev_month = first - timedelta(days=1)
    next_month = first + timedelta(days=32)
    return first, prev_month.replace(day=1), next_month.replace(day=1)


def notify_users(users, subject, body):
    recipients = [u.email for u in users if u.email]
    if not recipients:
        return
    try:
        send_mail(f"[{settings.SITE_NAME}] {subject}", body, settings.DEFAULT_FROM_EMAIL, recipients)
    except Exception:  # A notification must never break the request.
        logger.exception("Could not send notification %r", subject)


def _parents():
    return User.objects.filter(role=User.Role.PARENT, is_active=True)


# --- weeks & days ---------------------------------------------------------


def _greeting():
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _redirect_back(request, default, *args, trip_day=None, **kwargs):
    """Return to where a form asked for (next=home, or next=day for that day's page), else to `default`."""
    target = request.POST.get("next")
    if target == "home":
        return redirect("schedule:home")
    if target == "day" and trip_day:
        return redirect("schedule:day", day=trip_day.isoformat())
    return redirect(default, *args, **kwargs)


@login_required
def home(request):
    """Start page: greeting, today, the days ahead and the time account.
    Only the nanny gets the quick actions."""
    today = timezone.localdate()
    week_monday = engine.week_start(today)
    if today.weekday() < 4:
        coming_title, coming_start, coming_end = "Rest of the week", today + timedelta(days=1), week_monday + timedelta(days=6)
    else:
        # From Friday, the rest of the week is the weekend: look ahead to the next working week.
        coming_title = "Next week"
        coming_start = week_monday + timedelta(days=7)
        coming_end = coming_start + timedelta(days=4)
    days = engine.with_ytd(engine.compute_days(today, coming_end, today), today)
    info, coming = days[0], [d for d in days if d.date >= coming_start]
    can_act = request.user.is_nanny
    return render(
        request,
        "schedule/home.html",
        {
            "greeting": _greeting(),
            "d": info,
            "coming": coming,
            "coming_title": coming_title,
            "coming_week": engine.week_start(coming_start),
            "can_act": can_act,
            "today_can_confirm": can_act and info.can_confirm,
            "today_trips": Trip.objects.filter(date=today),
            "to_confirm": engine.days_to_confirm(today) if can_act else [],
            "balance": engine.balance(today=today),
            "has_contract": Contract.objects.exists(),
        },
    )


WEEK_SESSION_KEY = "selected_week"


def selected_week(request, day):
    """The week to show. A week opened explicitly (a date in the URL) is remembered for this
    browser for the rest of the day, so going to another tab and back keeps it; otherwise the
    current week. (Taken from the chef app.)"""
    today = timezone.localdate()
    if day:
        start = engine.week_start(parse_date(day))
        request.session[WEEK_SESSION_KEY] = {"start": start.isoformat(), "on": today.isoformat()}
        return start
    saved = request.session.get(WEEK_SESSION_KEY) or {}
    if saved.get("on") == today.isoformat():
        try:
            return date.fromisoformat(saved["start"])
        except (KeyError, ValueError):
            pass
    return engine.week_start(today)


@login_required
def week(request, day=None):
    today = timezone.localdate()
    start = selected_week(request, day)
    days = engine.with_ytd(engine.compute_days(start, start + timedelta(days=6), today), today)
    return render(
        request,
        "schedule/week.html",
        {
            "days": days,
            "start": start,
            "end": start + timedelta(days=6),
            "prev": start - timedelta(days=7),
            "next": start + timedelta(days=7),
            "this_week": engine.week_start(today),
            "week_delta": sum(d.delta for d in days),
            "week_worked": sum(d.worked for d in days if d.counted),
            "week_planned": sum(d.planned_minutes for d in days),
        },
    )


@login_required
def day_edit(request, day):
    day = parse_date(day)
    record = Day.objects.filter(date=day).first() or Day(date=day)
    is_parent = request.user.is_parent
    info = engine.compute_days(day, day)[0]
    default_plan = engine.default_plan(info)

    # Show the plan that is in effect, including the contract's default times.
    initial = {}
    if not record.planned_start and info.planned:
        initial = {"planned_start": info.planned[0], "planned_end": info.planned[1]}
    can_confirm = info.can_confirm

    if request.method == "POST" and "confirm" in request.POST and can_confirm:
        record.actual_start, record.actual_end = info.planned
        record.updated_by = request.user
        record.save()
        messages.success(request, f"Confirmed {day:%a %d.%m.} as planned.")
        return _redirect_back(request, "schedule:week_of", day=day.isoformat())

    form = DayForm(request.POST or None, instance=record, initial=initial, is_parent=is_parent)

    if request.method == "POST" and form.is_valid():
        before = notify.snapshot(day, day)
        record = form.save(commit=False)
        # Don't store the default times, so later contract changes still apply to this day.
        if default_plan and (record.planned_start, record.planned_end) == default_plan:
            record.planned_start = record.planned_end = None
        if record.is_blank:
            if record.pk:
                record.delete()
        else:
            record.updated_by = request.user
            record.save()
        notify.plan_changed(request, before)
        messages.success(request, f"Saved {day:%a %d.%m.}")
        return _redirect_back(request, "schedule:week_of", day=day.isoformat())

    return render(
        request,
        "schedule/day.html",
        {
            "info": info, "form": form, "day": day, "default_plan": default_plan, "can_confirm": can_confirm,
            "trips": Trip.objects.filter(date=day),
        },
    )


@require_POST
@login_required
def confirm_all(request):
    """Confirm every past day that has no times yet as worked as planned."""
    days = engine.days_to_confirm()
    for info in days:
        record = info.record or Day(date=info.date)
        record.actual_start, record.actual_end = info.planned
        record.updated_by = request.user
        record.save()
    messages.success(request, f"Confirmed {len(days)} day{'s' if len(days) != 1 else ''} as planned.")
    return _redirect_back(request, "schedule:week")


@login_required
def extra_edit(request, pk=None):
    extra = get_object_or_404(Extra, pk=pk) if pk else None
    if request.method == "POST" and extra and "delete" in request.POST:
        day = extra.date
        extra.delete()
        messages.success(request, "Removed.")
        return redirect("schedule:week_of", day=day.isoformat())

    initial = {}
    if not extra:
        initial["date"] = parse_date(request.GET["date"]) if request.GET.get("date") else timezone.localdate()
        initial.update(start="19:00", end="23:00")
    form = ExtraForm(request.POST or None, instance=extra, initial=initial)
    if request.method == "POST" and form.is_valid():
        extra = form.save(commit=False)
        extra.updated_by = request.user
        extra.save()
        messages.success(request, f"Saved {extra.get_kind_display().lower()} on {extra.date:%a %d.%m.}")
        return redirect("schedule:week_of", day=extra.date.isoformat())
    return render(request, "schedule/extra.html", {"form": form, "extra": extra})


# --- vacation -------------------------------------------------------------


@login_required
def vacation(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
    except ValueError:
        raise Http404("Invalid year")

    form = VacationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        vac = form.save(commit=False)
        vac.requested_by = request.user
        if request.user.is_parent:
            vac.status = Vacation.Status.APPROVED
            vac.decided_by = request.user
            vac.decided_at = timezone.now()
        vac.save()
        days = engine.vacation_workdays(vac.start_date, vac.end_date)
        if vac.status == Vacation.Status.PENDING:
            notify_users(
                _parents(),
                "Vacation request",
                f"{request.user} asked for vacation from {vac.start_date:%d.%m.%Y} to {vac.end_date:%d.%m.%Y} "
                f"({days} working days).\n\n{vac.note}\n\n{request.build_absolute_uri(reverse('schedule:vacation'))}",
            )
            messages.success(request, "Request sent.")
        else:
            messages.success(request, "Vacation added.")
        return redirect(f"{reverse('schedule:vacation')}?year={vac.start_date.year}")

    vacations = Vacation.objects.filter(start_date__year__lte=year, end_date__year__gte=year)
    rows = [(v, engine.vacation_workdays(v.start_date, v.end_date)) for v in vacations]
    return render(
        request,
        "schedule/vacation.html",
        {
            "form": form,
            "year": year,
            "summary": engine.vacation_summary(year, today),
            "rows": rows,
        },
    )


@require_POST
@login_required
def vacation_action(request, pk):
    vac = get_object_or_404(Vacation, pk=pk)
    action = request.POST.get("action")
    back = f"{reverse('schedule:vacation')}?year={vac.start_date.year}"

    if action == "cancel":
        own_pending = vac.requested_by_id == request.user.pk and vac.status == Vacation.Status.PENDING
        if not (request.user.is_parent or own_pending):
            raise PermissionDenied
        vac.delete()
        messages.success(request, "Vacation removed.")
        return redirect(back)

    if action not in ("approve", "reject") or not request.user.is_parent:
        raise PermissionDenied
    vac.status = Vacation.Status.APPROVED if action == "approve" else Vacation.Status.REJECTED
    vac.decided_by = request.user
    vac.decided_at = timezone.now()
    vac.save()
    if vac.requested_by and vac.requested_by != request.user:
        notify_users(
            [vac.requested_by],
            f"Vacation {vac.get_status_display().lower()}",
            f"Your vacation from {vac.start_date:%d.%m.%Y} to {vac.end_date:%d.%m.%Y} "
            f"was {vac.get_status_display().lower()} by {request.user}.",
        )
    messages.success(request, f"Vacation {vac.get_status_display().lower()}.")
    return redirect(back)


# --- trips ----------------------------------------------------------------


@login_required
def trips(request, year=None, month=None):
    today = timezone.localdate()
    year, month = year or today.year, month or today.month
    first, prev_month, next_month = _month_bounds(year, month)

    form = TripForm(request.POST or None, initial={"date": today})
    if request.method == "POST" and form.is_valid():
        trip = form.save(commit=False)
        trip.updated_by = request.user
        trip.save()
        messages.success(request, "Trip saved.")
        return _redirect_back(
            request, "schedule:trips_month", trip_day=trip.date, year=trip.date.year, month=trip.date.month
        )

    summary_trips = Trip.objects.filter(date__year=year, date__month=month)
    contract = Contract.objects.filter(start_date__lte=first).first() or Contract.objects.last()
    total_km = sum(t.km for t in summary_trips)
    return render(
        request,
        "schedule/trips.html",
        {
            "form": form,
            "trips": summary_trips,
            "month": first,
            "prev": prev_month,
            "next": next_month,
            "total_km": total_km,
            "total_amount": engine.money(total_km * contract.km_rate) if contract else None,
        },
    )


@login_required
def trip_edit(request, pk):
    trip = get_object_or_404(Trip, pk=pk)
    back = reverse("schedule:trips_month", args=[trip.date.year, trip.date.month])
    if request.method == "POST" and "delete" in request.POST:
        trip.delete()
        messages.success(request, "Trip removed.")
        return redirect(back)
    form = TripForm(request.POST or None, instance=trip)
    if request.method == "POST" and form.is_valid():
        trip = form.save(commit=False)
        trip.updated_by = request.user
        trip.save()
        messages.success(request, "Trip saved.")
        return redirect("schedule:trips_month", year=trip.date.year, month=trip.date.month)
    return render(request, "schedule/trip_edit.html", {"form": form, "trip": trip, "back": back})


# --- planning (parents) ---------------------------------------------------


@parent_required
def plan(request):
    today = timezone.localdate()
    bulk = BulkPlanForm(request.POST if "bulk" in request.POST else None)
    holiday_form = HolidayForm(request.POST if "holiday" in request.POST else None)

    if bulk.is_bound and bulk.is_valid():
        before = notify.snapshot(bulk.cleaned_data["start"], bulk.cleaned_data["end"])
        changed = _apply_plan(bulk.cleaned_data, request.user)
        notify.plan_changed(request, before)
        messages.success(request, f"Updated {changed} day{'s' if changed != 1 else ''}.")
        return redirect("schedule:week_of", day=bulk.cleaned_data["start"].isoformat())

    if holiday_form.is_bound and holiday_form.is_valid():
        day = holiday_form.cleaned_data["date"]
        before = notify.snapshot(day, day)
        holiday = holiday_form.save()
        notify.plan_changed(request, before)
        messages.success(request, f"Added {holiday}.")
        return redirect("schedule:plan")

    if request.method == "POST" and "delete_holiday" in request.POST:
        holiday = Holiday.objects.filter(pk=request.POST["delete_holiday"]).first()
        if holiday:
            before = notify.snapshot(holiday.date, holiday.date)
            holiday.delete()
            notify.plan_changed(request, before)
        messages.success(request, "Holiday removed.")
        return redirect("schedule:plan")

    return render(
        request,
        "schedule/plan.html",
        {
            "bulk": bulk,
            "holiday_form": holiday_form,
            "holidays": Holiday.objects.filter(date__gte=today.replace(month=1, day=1)),
        },
    )


def _apply_plan(data, user):
    """Apply a bulk plan to every matching day. Logged times are kept."""
    action = data["action"]
    changed = 0
    for info in engine.compute_days(data["start"], data["end"]):
        if info.contract is None:
            continue
        if not data["include_weekends"] and info.date.weekday() not in info.contract.workday_numbers:
            continue
        record = info.record or Day(date=info.date)
        if action == BulkPlanForm.NOT_NEEDED:
            record.kind = Day.Kind.NOT_NEEDED
            record.planned_start = record.planned_end = None
        elif action == BulkPlanForm.TIMES:
            record.kind = Day.Kind.REGULAR
            record.planned_start, record.planned_end = data["planned_start"], data["planned_end"]
        else:
            record.kind = Day.Kind.REGULAR
            record.planned_start = record.planned_end = None
        if data["note"] or action == BulkPlanForm.RESET:
            record.note = data["note"]
        if record.is_blank:
            if record.pk:
                record.delete()
        else:
            record.updated_by = user
            record.save()
        changed += 1
    return changed


# --- admin screen (parents) -----------------------------------------------


@parent_required
def manage(request):
    """People and contract terms, without the technical Django admin."""
    people = User.objects.order_by("-is_active", "role", "name", "email")
    return render(request, "schedule/manage.html", {"people": people, "contracts": Contract.objects.all()})


@parent_required
def person_edit(request, pk=None):
    person = get_object_or_404(User, pk=pk) if pk else None
    form = PersonForm(request.POST or None, instance=person, editor=request.user,
                      initial={} if person else {"role": User.Role.NANNY, "is_active": True})
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"Saved {saved}.")
        return redirect("schedule:manage")
    return render(request, "schedule/person_edit.html", {"form": form, "person": person})


@parent_required
def contract_edit(request, pk=None):
    """Edit terms, or (without pk) add new terms from a date, starting from the latest ones."""
    contract = get_object_or_404(Contract, pk=pk) if pk else None
    if contract is None:
        latest = Contract.objects.first()
        contract = Contract(
            **{f.name: getattr(latest, f.name) for f in Contract._meta.fields if f.name not in ("id", "start_date", "end_date")}
        ) if latest else Contract()
    form = ContractForm(request.POST or None, instance=contract)
    if request.method == "POST" and "delete" in request.POST and contract.pk:
        if Contract.objects.count() == 1:
            messages.error(request, "The only contract can't be removed.")
        else:
            contract.delete()
            messages.success(request, "Terms removed.")
        return redirect("schedule:manage")
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"Saved the terms valid from {saved.start_date:%d.%m.%Y}.")
        return redirect("schedule:manage")
    return render(request, "schedule/contract_edit.html", {"form": form, "contract": contract})


# --- month overview (parents) ---------------------------------------------


@parent_required
def month(request, year=None, month=None):
    today = timezone.localdate()
    year, month = year or today.year, month or today.month
    first, prev_month, next_month = _month_bounds(year, month)

    form = AdjustmentForm(request.POST or None, initial={"date": min(today, first.replace(day=28))})
    if request.method == "POST" and "delete_adjustment" in request.POST:
        Adjustment.objects.filter(pk=request.POST["delete_adjustment"]).delete()
        messages.success(request, "Adjustment removed.")
        return redirect("schedule:month_of", year=year, month=month)
    if request.method == "POST" and form.is_valid():
        Adjustment.objects.create(
            date=form.cleaned_data["date"],
            minutes=form.minutes,
            paid_out=form.cleaned_data["kind"] == AdjustmentForm.PAYOUT,
            note=form.cleaned_data["note"],
            created_by=request.user,
        )
        messages.success(request, "Adjustment saved.")
        d = form.cleaned_data["date"]
        return redirect("schedule:month_of", year=d.year, month=d.month)

    return render(
        request,
        "schedule/month.html",
        {
            "s": engine.month_summary(year, month, today),
            "form": form,
            "prev": prev_month,
            "next": next_month,
        },
    )
