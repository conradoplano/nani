"""
Hour balance, pay and vacation calculations.

Rules (from the contract and what we agreed):
- Each regular workday the nanny owes the contract's daily hours (Mon–Fri 15–19 = 4 h).
- Past workdays without a logged time count as worked as planned, so she only
  has to enter deviations. Logged times always win.
- "Not needed" days are still owed: they go into the balance as minus hours
  that she works off later.
- Holidays, approved vacation and sick days are not owed (0 h target).
- Evening engagements are either paid or credited to the balance as time off.
- Overnights are always paid: hourly rate outside the night window
  (22:00–06:00) plus the night flat rate.
- A day only affects the balance once it is in the past or has been logged.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import floor

from django.utils import timezone

from .models import Adjustment, Contract, Day, Extra, Holiday, Trip, Vacation, span_minutes

AUTO_BREAK_AFTER = 6 * 60
AUTO_BREAK = 30


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def minutes_to_money(minutes, rate):
    return money(Decimal(minutes) * rate / 60)


def worked_minutes(start, end, break_minutes=None):
    span = span_minutes(start, end)
    if break_minutes is None:
        break_minutes = AUTO_BREAK if span > AUTO_BREAK_AFTER else 0
    return max(span - break_minutes, 0)


def _overlap(a_start, a_end, b_start, b_end):
    return max((min(a_end, b_end) - max(a_start, b_start)).total_seconds() // 60, 0)


class Status:
    OUTSIDE = "outside"
    OFF = "off"
    WORK = "work"
    NOT_NEEDED = "not_needed"
    SICK = "sick"
    VACATION = "vacation"
    HOLIDAY = "holiday"

    LABELS = {
        OUTSIDE: "No contract",
        OFF: "Day off",
        WORK: "Scheduled",
        NOT_NEEDED: "No care needed",
        SICK: "Sick",
        VACATION: "Vacation",
        HOLIDAY: "Holiday",
    }


@dataclass
class ExtraInfo:
    extra: Extra
    minutes: int
    paid_minutes: int
    time_off_minutes: int
    flat: Decimal
    amount: Decimal


def evaluate_extra(extra, contract):
    total = extra.minutes
    flat = Decimal("0")
    time_off = 0
    if extra.kind == Extra.Kind.OVERNIGHT:
        start = extra.start_dt
        end = start + timedelta(minutes=total)
        night = 0
        for offset in (-1, 0):
            night_start = datetime.combine(extra.date + timedelta(days=offset), contract.night_from)
            night_end = datetime.combine(extra.date + timedelta(days=offset + 1), contract.night_until)
            night += _overlap(start, end, night_start, night_end)
        paid = total - int(night)
        flat = contract.night_flat_rate
    elif extra.settlement == Extra.Settlement.PAID:
        paid = total
    else:
        paid = 0
        time_off = total
    amount = minutes_to_money(paid, contract.hourly_rate) + flat
    return ExtraInfo(extra, total, paid, time_off, flat, money(amount))


@dataclass
class DayInfo:
    date: date
    contract: Contract | None
    status: str
    today: date
    record: Day | None = None
    holiday: Holiday | None = None
    vacation: Vacation | None = None
    pending_vacation: Vacation | None = None
    planned: tuple | None = None
    target: int = 0
    worked: int = 0
    logged: bool = False
    extras: list = field(default_factory=list)

    @property
    def label(self):
        return Status.LABELS[self.status]

    @property
    def is_today(self):
        return self.date == self.today

    @property
    def is_future(self):
        return self.date > self.today

    @property
    def counted(self):
        """Whether the regular hours of this day affect the balance yet."""
        return self.contract is not None and (self.date < self.today or self.logged)

    @property
    def extras_counted(self):
        return self.contract is not None and self.date <= self.today

    @property
    def time_off_credit(self):
        return sum(e.time_off_minutes for e in self.extras) if self.extras_counted else 0

    @property
    def delta(self):
        regular = self.worked - self.target if self.counted else 0
        return regular + self.time_off_credit

    @property
    def planned_minutes(self):
        if not self.planned or self.status in (Status.NOT_NEEDED, Status.SICK):
            return 0
        return worked_minutes(*self.planned, self.record.break_minutes if self.record else None)

    @property
    def actual(self):
        rec = self.record
        return (rec.actual_start, rec.actual_end) if rec and rec.actual_start else None

    @property
    def note(self):
        return self.record.note if self.record else ""


def _contract_lookup(contracts):
    ordered = sorted(contracts, key=lambda c: c.start_date, reverse=True)

    def lookup(day):
        for contract in ordered:
            if contract.covers(day):
                return contract
        return None

    return lookup


def compute_days(start, end, today=None):
    """DayInfo for every date from start to end (inclusive)."""
    today = today or timezone.localdate()
    contract_for = _contract_lookup(Contract.objects.all())
    records = {d.date: d for d in Day.objects.filter(date__range=(start, end))}
    holidays = {h.date: h for h in Holiday.objects.filter(date__range=(start, end))}
    vacations = list(
        Vacation.objects.filter(start_date__lte=end, end_date__gte=start).exclude(status=Vacation.Status.REJECTED)
    )
    extras = {}
    for extra in Extra.objects.filter(date__range=(start, end)):
        extras.setdefault(extra.date, []).append(extra)

    days = []
    current = start
    while current <= end:
        days.append(_day_info(current, today, contract_for(current), records, holidays, vacations, extras))
        current += timedelta(days=1)
    return days


def _day_info(day, today, contract, records, holidays, vacations, extras):
    rec = records.get(day)
    holiday = holidays.get(day)
    approved = next((v for v in vacations if v.status == Vacation.Status.APPROVED and v.covers(day)), None)
    pending = next((v for v in vacations if v.status == Vacation.Status.PENDING and v.covers(day)), None)
    info = DayInfo(day, contract, Status.OUTSIDE, today, rec, holiday, approved, pending)
    if contract is None:
        return info

    is_workday = day.weekday() in contract.workday_numbers
    kind = rec.kind if rec else Day.Kind.REGULAR
    explicit_plan = (rec.planned_start, rec.planned_end) if rec and rec.planned_start else None

    if holiday:
        info.status = Status.HOLIDAY
    elif approved and is_workday:
        info.status = Status.VACATION
    elif kind == Day.Kind.SICK:
        info.status = Status.SICK
    elif kind == Day.Kind.NOT_NEEDED:
        info.status = Status.NOT_NEEDED
        info.target = contract.daily_minutes if is_workday else 0
    elif is_workday or explicit_plan or (rec and rec.actual_start):
        info.status = Status.WORK
        info.target = contract.daily_minutes if is_workday else 0
    else:
        info.status = Status.OFF

    if explicit_plan:
        info.planned = explicit_plan
    elif info.status == Status.WORK and is_workday:
        info.planned = (contract.default_start, contract.default_end)

    if rec and rec.actual_start:
        info.logged = True
        info.worked = worked_minutes(rec.actual_start, rec.actual_end, rec.break_minutes)
    elif info.planned and info.status not in (Status.NOT_NEEDED, Status.SICK) and day < today:
        info.worked = worked_minutes(*info.planned, rec.break_minutes if rec else None)

    info.extras = [evaluate_extra(e, contract) for e in extras.get(day, [])]
    return info


def default_plan(info):
    """The contract's regular times for this day, or None if it isn't a regular workday."""
    contract = info.contract
    if contract is None or info.date.weekday() not in contract.workday_numbers:
        return None
    return (contract.default_start, contract.default_end)


def first_contract_date():
    first = Contract.objects.order_by("start_date").first()
    return first.start_date if first else None


def balance(until=None, today=None):
    """Hour balance in minutes up to and including `until` (default: today)."""
    today = today or timezone.localdate()
    until = min(until or today, today)
    start = first_contract_date()
    if start is None or until < start:
        return 0
    total = sum(d.delta for d in compute_days(start, until, today))
    total += sum(Adjustment.objects.filter(date__lte=until).values_list("minutes", flat=True))
    return total


def week_start(day):
    return day - timedelta(days=day.weekday())


# --- vacation -------------------------------------------------------------


def vacation_workdays(start, end, today=None):
    """Number of days in the range that would be taken from the vacation allowance."""
    return sum(1 for d in compute_days(start, end, today) if _is_vacation_day(d))


def _is_vacation_day(info):
    return (
        info.contract is not None
        and info.holiday is None
        and info.date.weekday() in info.contract.workday_numbers
    )


def entitlement(year):
    """
    Vacation days for the calendar year. In a partial year it is 1/12 per full
    month employed; fractions of at least half a day round up (§5 BUrlG).
    """
    contracts = list(Contract.objects.order_by("start_date"))
    if not contracts:
        return Decimal("0")
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    emp_start = contracts[0].start_date
    emp_end = contracts[-1].end_date
    if emp_start > dec31 or (emp_end and emp_end < jan1):
        return Decimal("0")

    contract_for = _contract_lookup(contracts)
    per_year = contract_for(max(emp_start, jan1)).vacation_days_per_year
    if emp_start <= jan1 and (emp_end is None or emp_end >= dec31):
        return per_year

    months = 0
    for month in range(1, 13):
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        if emp_start <= first and (emp_end is None or emp_end >= last):
            months += 1
    value = per_year * months / 12
    if value - floor(value) >= Decimal("0.5"):
        value = Decimal(floor(value) + 1)
    return value.quantize(Decimal("0.01"))


@dataclass
class VacationSummary:
    year: int
    entitlement: Decimal
    taken: int
    pending: int

    @property
    def remaining(self):
        return self.entitlement - self.taken


def vacation_summary(year, today=None):
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    taken = pending = 0
    for info in compute_days(jan1, dec31, today):
        if not _is_vacation_day(info):
            continue
        if info.vacation:
            taken += 1
        elif info.pending_vacation:
            pending += 1
    return VacationSummary(year, entitlement(year), taken, pending)


# --- month ----------------------------------------------------------------


@dataclass
class MonthSummary:
    first: date
    last: date
    days: list
    salary: Decimal
    extras: list
    extra_pay: Decimal
    trips: list
    km: Decimal
    km_amount: Decimal
    adjustments: list
    payout_minutes: int
    payout_amount: Decimal
    balance_start: int
    balance_end: int

    @property
    def target(self):
        return sum(d.target for d in self.days if d.counted)

    @property
    def worked(self):
        return sum(d.worked for d in self.days if d.counted)

    @property
    def time_off_credit(self):
        return sum(d.time_off_credit for d in self.days)

    @property
    def adjustment_minutes(self):
        return sum(a.minutes for a in self.adjustments)

    @property
    def delta(self):
        return sum(d.delta for d in self.days) + self.adjustment_minutes

    @property
    def vacation_days(self):
        return sum(1 for d in self.days if d.status == Status.VACATION)

    @property
    def sick_days(self):
        return sum(1 for d in self.days if d.status == Status.SICK)

    @property
    def not_needed_days(self):
        return sum(1 for d in self.days if d.status == Status.NOT_NEEDED)

    @property
    def gross_total(self):
        return self.salary + self.extra_pay + self.payout_amount

    @property
    def total(self):
        """Gross pay plus the (tax-free) mileage reimbursement."""
        return self.gross_total + self.km_amount


def month_summary(year, month, today=None):
    today = today or timezone.localdate()
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    days = compute_days(first, last, today)
    contract_for = _contract_lookup(Contract.objects.all())

    salary = sum(
        (d.contract.monthly_salary / len(days) for d in days if d.contract is not None), Decimal("0")
    )
    extras = [e for d in days for e in d.extras]
    trips = list(Trip.objects.filter(date__range=(first, last)))
    km = sum((t.km for t in trips), Decimal("0"))
    km_amount = sum(
        (t.km * contract_for(t.date).km_rate for t in trips if contract_for(t.date)), Decimal("0")
    )
    adjustments = list(Adjustment.objects.filter(date__range=(first, last)))
    payout_minutes = -sum(a.minutes for a in adjustments if a.paid_out)
    payout_amount = sum(
        (minutes_to_money(-a.minutes, contract_for(a.date).hourly_rate)
         for a in adjustments if a.paid_out and contract_for(a.date)),
        Decimal("0"),
    )
    return MonthSummary(
        first=first,
        last=last,
        days=days,
        salary=money(salary),
        extras=extras,
        extra_pay=sum((e.amount for e in extras), Decimal("0")),
        trips=trips,
        km=km,
        km_amount=money(km_amount),
        adjustments=adjustments,
        payout_minutes=payout_minutes,
        payout_amount=money(payout_amount),
        balance_start=balance(first - timedelta(days=1), today),
        balance_end=balance(last, today),
    )
