from django.contrib import admin

from .models import Adjustment, Contract, Day, Extra, Holiday, Trip, Vacation


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("start_date", "end_date", "nanny", "workdays", "default_start", "default_end", "monthly_salary")
    fieldsets = (
        (None, {"fields": ("nanny", "start_date", "end_date")}),
        ("Working time", {"fields": ("workdays", "default_start", "default_end")}),
        ("Pay", {"fields": ("monthly_salary", "hourly_rate", "night_flat_rate", "night_from", "night_until", "km_rate")}),
        ("Vacation", {"fields": ("vacation_days_per_year",)}),
    )


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name")
    date_hierarchy = "date"


@admin.register(Day)
class DayAdmin(admin.ModelAdmin):
    list_display = ("date", "kind", "planned_start", "planned_end", "actual_start", "actual_end", "note", "updated_by")
    list_filter = ("kind",)
    date_hierarchy = "date"


@admin.register(Extra)
class ExtraAdmin(admin.ModelAdmin):
    list_display = ("date", "kind", "start", "end", "settlement", "note")
    list_filter = ("kind", "settlement")
    date_hierarchy = "date"


@admin.register(Vacation)
class VacationAdmin(admin.ModelAdmin):
    list_display = ("start_date", "end_date", "status", "requested_by", "decided_by")
    list_filter = ("status",)


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ("date", "purpose", "km")
    date_hierarchy = "date"


@admin.register(Adjustment)
class AdjustmentAdmin(admin.ModelAdmin):
    list_display = ("date", "minutes", "paid_out", "note", "created_by")
