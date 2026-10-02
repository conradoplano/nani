from decimal import Decimal

from django import forms

from .models import Day, Extra, Holiday, Trip, Vacation


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        super().__init__(format="%H:%M", **kwargs)


class DayForm(forms.ModelForm):
    PARENT_FIELDS = ["kind", "planned_start", "planned_end", "actual_start", "actual_end", "break_minutes", "note"]
    NANNY_FIELDS = ["kind", "actual_start", "actual_end", "break_minutes", "note"]

    class Meta:
        model = Day
        fields = ["kind", "planned_start", "planned_end", "actual_start", "actual_end", "break_minutes", "note"]
        labels = {
            "kind": "Day type",
            "planned_start": "Planned from",
            "planned_end": "Planned until",
            "actual_start": "Worked from",
            "actual_end": "Worked until",
            "break_minutes": "Break (minutes)",
        }
        widgets = {
            "planned_start": TimeInput(),
            "planned_end": TimeInput(),
            "actual_start": TimeInput(),
            "actual_end": TimeInput(),
            "break_minutes": forms.NumberInput(attrs={"inputmode": "numeric", "placeholder": "auto"}),
        }

    def __init__(self, *args, is_parent=False, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = self.PARENT_FIELDS if is_parent else self.NANNY_FIELDS
        for name in list(self.fields):
            if name not in allowed:
                del self.fields[name]
        if not is_parent:
            # Only the parents can decide that a day is not needed.
            choices = [c for c in Day.Kind.choices if c[0] != Day.Kind.NOT_NEEDED]
            if self.instance.kind == Day.Kind.NOT_NEEDED:
                self.fields["kind"].disabled = True
            else:
                self.fields["kind"].choices = choices


class ExtraForm(forms.ModelForm):
    class Meta:
        model = Extra
        fields = ["date", "kind", "start", "end", "settlement", "note"]
        widgets = {"date": DateInput(), "start": TimeInput(), "end": TimeInput()}
        labels = {"settlement": "Compensation"}


class VacationForm(forms.ModelForm):
    class Meta:
        model = Vacation
        fields = ["start_date", "end_date", "note"]
        labels = {"start_date": "First day", "end_date": "Last day"}
        widgets = {"start_date": DateInput(), "end_date": DateInput()}


class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ["date", "purpose", "km"]
        widgets = {
            "date": DateInput(),
            "km": forms.NumberInput(attrs={"inputmode": "decimal", "step": "0.1", "min": "0"}),
        }


class HolidayForm(forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {"date": DateInput()}


class BulkPlanForm(forms.Form):
    NOT_NEEDED = "not_needed"
    TIMES = "times"
    RESET = "reset"
    ACTIONS = [
        (NOT_NEEDED, "No care needed (the hours can be made up later)"),
        (TIMES, "Different times"),
        (RESET, "Back to the normal schedule"),
    ]

    start = forms.DateField(label="From", widget=DateInput())
    end = forms.DateField(label="Until", widget=DateInput())
    action = forms.ChoiceField(choices=ACTIONS, widget=forms.RadioSelect, initial=NOT_NEEDED)
    planned_start = forms.TimeField(label="From time", required=False, widget=TimeInput())
    planned_end = forms.TimeField(label="Until time", required=False, widget=TimeInput())
    include_weekends = forms.BooleanField(
        required=False, help_text="Otherwise only the contract's regular workdays are changed."
    )
    note = forms.CharField(max_length=200, required=False)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            self.add_error("end", "This is before the first day.")
        if data.get("start") and data.get("end") and (data["end"] - data["start"]).days > 366:
            self.add_error("end", "Plan at most one year at a time.")
        if data.get("action") == self.TIMES and not (data.get("planned_start") and data.get("planned_end")):
            self.add_error("planned_start", "Enter both times.")
        return data


class AdjustmentForm(forms.Form):
    PAYOUT = "payout"
    CORRECTION = "correction"

    date = forms.DateField(widget=DateInput())
    kind = forms.ChoiceField(
        choices=[
            (PAYOUT, "Pay out extra hours (removes them from the time account)"),
            (CORRECTION, "Correction (+ adds, − removes)"),
        ],
        widget=forms.RadioSelect,
        initial=PAYOUT,
    )
    hours = forms.DecimalField(max_digits=6, decimal_places=2, widget=forms.NumberInput(attrs={"step": "0.25"}))
    note = forms.CharField(max_length=200, required=False)

    def clean(self):
        data = super().clean()
        if data.get("kind") == self.PAYOUT and data.get("hours") is not None and data["hours"] <= 0:
            self.add_error("hours", "Enter the number of hours to pay out.")
        return data

    @property
    def minutes(self):
        minutes = int((self.cleaned_data["hours"] * Decimal(60)).to_integral_value())
        return -abs(minutes) if self.cleaned_data["kind"] == self.PAYOUT else minutes
