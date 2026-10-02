from datetime import date, datetime, time
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def span_minutes(start, end):
    """Minutes from start to end; an end at or before start means the next day."""
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if e <= s:
        e += 24 * 60
    return e - s


class Contract(models.Model):
    """
    Terms from the employment contract. A new row with a later start date
    replaces the previous terms (e.g. a raise or a change in hours).
    Defaults follow the contract signed for 01.10.2026.
    """

    nanny = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        limit_choices_to={"role": "nanny"},
        related_name="contracts",
    )
    start_date = models.DateField(help_text="First day these terms apply.")
    end_date = models.DateField(null=True, blank=True, help_text="Last day of employment, if known.")

    workdays = models.CharField(
        max_length=13,
        default="0,1,2,3,4",
        help_text="Regular working weekdays, 0 = Monday … 6 = Sunday, comma separated.",
    )
    default_start = models.TimeField(default=time(15, 0))
    default_end = models.TimeField(default=time(19, 0))

    monthly_salary = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("2166.67"))
    hourly_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("25.00"))
    night_flat_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("50.00"), help_text="Paid per overnight stay."
    )
    night_from = models.TimeField(default=time(22, 0), help_text="Overnight hours from here are not paid hourly.")
    night_until = models.TimeField(default=time(6, 0), help_text="…until this time the next morning.")
    km_rate = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("0.30"))

    vacation_days_per_year = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("25"))

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"Contract from {self.start_date:%d.%m.%Y}"

    def clean(self):
        try:
            days = self.workday_numbers
        except ValueError:
            raise ValidationError({"workdays": "Use numbers 0–6 separated by commas."})
        if any(d < 0 or d > 6 for d in days):
            raise ValidationError({"workdays": "Use numbers 0–6 separated by commas."})
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date is before the start date."})

    @property
    def workday_numbers(self):
        return {int(x) for x in self.workdays.split(",") if x.strip()}

    @property
    def workday_names(self):
        return ", ".join(WEEKDAYS[d] for d in sorted(self.workday_numbers))

    @property
    def daily_minutes(self):
        return span_minutes(self.default_start, self.default_end)

    @property
    def weekly_minutes(self):
        return self.daily_minutes * len(self.workday_numbers)

    def covers(self, day):
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)


class TrackedModel(models.Model):
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Holiday(models.Model):
    """Public holiday entered by the parents. No work is owed and it doesn't use vacation."""

    date = models.DateField(unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date:%d.%m.%Y} {self.name}"


class Day(TrackedModel):
    """
    One calendar day: the plan (set by the parents) and what actually
    happened (logged by the nanny). Rows only exist for days that differ
    from the contract's default schedule.
    """

    class Kind(models.TextChoices):
        REGULAR = "regular", "Regular"
        NOT_NEEDED = "not_needed", "No care needed"
        SICK = "sick", "Sick"

    date = models.DateField(unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.REGULAR)
    planned_start = models.TimeField(null=True, blank=True)
    planned_end = models.TimeField(null=True, blank=True)
    actual_start = models.TimeField(null=True, blank=True)
    actual_end = models.TimeField(null=True, blank=True)
    break_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Leave empty for the legal default (30 min above 6 hours)."
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} ({self.get_kind_display()})"

    def clean(self):
        if bool(self.planned_start) != bool(self.planned_end):
            raise ValidationError("Enter both planned start and end, or neither.")
        if bool(self.actual_start) != bool(self.actual_end):
            raise ValidationError("Enter both start and end of the time worked, or neither.")

    @property
    def is_blank(self):
        """True when the row carries no information beyond the defaults."""
        return (
            self.kind == self.Kind.REGULAR
            and not self.planned_start
            and not self.actual_start
            and self.break_minutes is None
            and not self.note
        )


class Extra(TrackedModel):
    """An evening or overnight engagement on top of the regular hours (§3 of the contract)."""

    class Kind(models.TextChoices):
        EVENING = "evening", "Evening"
        OVERNIGHT = "overnight", "Overnight"

    class Settlement(models.TextChoices):
        PAID = "paid", "Paid"
        TIME_OFF = "time_off", "Time off"

    date = models.DateField(help_text="The day the engagement starts.")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.EVENING)
    start = models.TimeField()
    end = models.TimeField(help_text="An end before the start means the next morning.")
    settlement = models.CharField(
        max_length=10,
        choices=Settlement.choices,
        default=Settlement.TIME_OFF,
        help_text="Overnights are always paid.",
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date", "start"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.date} {self.start:%H:%M}–{self.end:%H:%M}"

    def save(self, *args, **kwargs):
        if self.kind == self.Kind.OVERNIGHT:
            self.settlement = self.Settlement.PAID
        super().save(*args, **kwargs)

    @property
    def start_dt(self):
        return datetime.combine(self.date, self.start)

    @property
    def minutes(self):
        return span_minutes(self.start, self.end)


class Vacation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Declined"

    start_date = models.DateField()
    end_date = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"Vacation {self.start_date}–{self.end_date} ({self.get_status_display()})"

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The last day is before the first day."})

    def covers(self, day):
        return self.start_date <= day <= self.end_date


class Trip(TrackedModel):
    """A drive with the nanny's private car ordered by the parents (§5 of the contract)."""

    date = models.DateField()
    purpose = models.CharField(max_length=200)
    km = models.DecimalField(max_digits=6, decimal_places=1)

    class Meta:
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.date} {self.purpose} ({self.km} km)"


class Adjustment(models.Model):
    """Manual change to the hour balance, e.g. paying out overtime or correcting an error."""

    date = models.DateField()
    minutes = models.IntegerField(help_text="Positive adds to the balance, negative subtracts.")
    paid_out = models.BooleanField(
        default=False, help_text="The removed hours are paid at the hourly rate with that month's salary."
    )
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.date} {self.minutes:+d} min"
