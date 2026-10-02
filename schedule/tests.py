from datetime import date, time
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import User

from . import engine
from .models import Adjustment, Contract, Day, Extra, Holiday, Trip, Vacation

# 1 Oct 2026 is a Thursday. Workdays in the first week: Thu 1, Fri 2, Mon 5, Tue 6, Wed 7.
START = date(2026, 10, 1)
TODAY = date(2026, 10, 8)


class EngineTests(TestCase):
    def setUp(self):
        Contract.objects.create(start_date=START)

    def balance(self):
        return engine.balance(today=TODAY)

    def test_unlogged_past_workdays_count_as_planned(self):
        self.assertEqual(self.balance(), 0)
        days = engine.compute_days(START, date(2026, 10, 7), TODAY)
        self.assertEqual(sum(d.worked for d in days), 5 * 240)

    def test_not_needed_day_is_minus(self):
        Day.objects.create(date=date(2026, 10, 2), kind=Day.Kind.NOT_NEEDED)
        self.assertEqual(self.balance(), -240)

    def test_logged_longer_day_is_plus(self):
        Day.objects.create(date=date(2026, 10, 5), actual_start=time(15), actual_end=time(20))
        self.assertEqual(self.balance(), 60)

    def test_automatic_break_above_six_hours(self):
        Day.objects.create(date=date(2026, 10, 5), actual_start=time(10), actual_end=time(17))
        self.assertEqual(self.balance(), 7 * 60 - 30 - 240)

    def test_planned_longer_day_counts_once_past(self):
        Day.objects.create(date=date(2026, 10, 6), planned_start=time(13), planned_end=time(19))
        self.assertEqual(self.balance(), 120)

    def test_weekend_work_is_plus(self):
        Day.objects.create(date=date(2026, 10, 3), actual_start=time(9), actual_end=time(12))
        self.assertEqual(self.balance(), 180)

    def test_holiday_vacation_and_sick_are_neutral(self):
        Holiday.objects.create(date=date(2026, 10, 6), name="Test holiday")
        Vacation.objects.create(start_date=date(2026, 10, 7), end_date=date(2026, 10, 7), status="approved")
        Day.objects.create(date=date(2026, 10, 5), kind=Day.Kind.SICK)
        self.assertEqual(self.balance(), 0)
        days = {d.date: d for d in engine.compute_days(START, date(2026, 10, 7), TODAY)}
        self.assertEqual(days[date(2026, 10, 6)].status, engine.Status.HOLIDAY)
        self.assertEqual(days[date(2026, 10, 7)].status, engine.Status.VACATION)
        self.assertEqual(days[date(2026, 10, 5)].status, engine.Status.SICK)

    def test_future_and_unlogged_today_not_counted(self):
        Day.objects.create(date=date(2026, 10, 9), kind=Day.Kind.NOT_NEEDED)
        self.assertEqual(self.balance(), 0)
        # Today counts once logged.
        Day.objects.create(date=TODAY, actual_start=time(15), actual_end=time(18))
        self.assertEqual(self.balance(), -60)

    def test_days_before_contract_ignored(self):
        Day.objects.create(date=date(2026, 9, 30), actual_start=time(15), actual_end=time(19))
        self.assertEqual(self.balance(), 0)

    def test_evening_time_off_adds_to_balance(self):
        Extra.objects.create(date=date(2026, 10, 5), kind="evening", start=time(19), end=time(22), settlement="time_off")
        self.assertEqual(self.balance(), 180)

    def test_paid_evening_does_not_change_balance(self):
        Extra.objects.create(date=date(2026, 10, 5), kind="evening", start=time(19), end=time(22), settlement="paid")
        self.assertEqual(self.balance(), 0)
        info = engine.evaluate_extra(Extra.objects.get(), Contract.objects.get())
        self.assertEqual(info.amount, Decimal("75.00"))

    def test_overnight_is_always_paid_outside_night_window_plus_flat(self):
        extra = Extra.objects.create(
            date=date(2026, 10, 5), kind="overnight", start=time(19), end=time(8), settlement="time_off"
        )
        self.assertEqual(extra.settlement, Extra.Settlement.PAID)
        info = engine.evaluate_extra(extra, Contract.objects.get())
        self.assertEqual(info.paid_minutes, 5 * 60)  # 19–22 and 06–08
        self.assertEqual(info.amount, Decimal("175.00"))  # 5 h × 25 € + 50 €
        self.assertEqual(self.balance(), 0)

    def test_payout_adjustment(self):
        Adjustment.objects.create(date=date(2026, 10, 5), minutes=-120, paid_out=True)
        self.assertEqual(self.balance(), -120)
        summary = engine.month_summary(2026, 10, TODAY)
        self.assertEqual(summary.payout_amount, Decimal("50.00"))

    def test_month_summary_pay(self):
        Trip.objects.create(date=date(2026, 10, 2), purpose="Pickup", km=Decimal("12.5"))
        Extra.objects.create(date=date(2026, 10, 5), kind="evening", start=time(19), end=time(21), settlement="paid")
        s = engine.month_summary(2026, 10, TODAY)
        self.assertEqual(s.salary, Decimal("2166.67"))
        self.assertEqual(s.extra_pay, Decimal("50.00"))
        self.assertEqual(s.km_amount, Decimal("3.75"))
        self.assertEqual(s.total, Decimal("2220.42"))

    def test_partial_month_salary(self):
        Contract.objects.all().delete()
        Contract.objects.create(start_date=date(2026, 10, 16))
        s = engine.month_summary(2026, 10, TODAY)
        self.assertEqual(s.salary, engine.money(Decimal("2166.67") * 16 / 31))

    def test_vacation_entitlement(self):
        self.assertEqual(engine.entitlement(2026), Decimal("6.25"))  # 3 full months
        self.assertEqual(engine.entitlement(2027), Decimal("25"))
        self.assertEqual(engine.entitlement(2025), Decimal("0"))

    def test_vacation_summary_skips_weekends_and_holidays(self):
        Holiday.objects.create(date=date(2026, 12, 25), name="Christmas Day")
        Holiday.objects.create(date=date(2026, 12, 26), name="Boxing Day")
        Vacation.objects.create(start_date=date(2026, 12, 21), end_date=date(2026, 12, 31), status="approved")
        Vacation.objects.create(start_date=date(2026, 11, 2), end_date=date(2026, 11, 3), status="pending")
        s = engine.vacation_summary(2026, TODAY)
        # 21–24 (4), 28–31 (4); 25 is a holiday, 26/27 are the weekend.
        self.assertEqual(s.taken, 8)
        self.assertEqual(s.pending, 2)
        self.assertEqual(s.remaining, Decimal("-1.75"))


class ViewTests(TestCase):
    def setUp(self):
        Contract.objects.create(start_date=START)
        self.parent = User.objects.create_user(email="parent@example.com", role="parent")
        self.nanny = User.objects.create_user(email="nanny@example.com", role="nanny")
        patcher = mock.patch("django.utils.timezone.localdate", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pages_render_for_parent(self):
        self.client.force_login(self.parent)
        for url in [
            reverse("schedule:week"),
            reverse("schedule:week_of", args=["2026-10-05"]),
            reverse("schedule:day", args=["2026-10-05"]),
            reverse("schedule:extra_new") + "?date=2026-10-05",
            reverse("schedule:vacation"),
            reverse("schedule:trips"),
            reverse("schedule:plan"),
            reverse("schedule:month"),
            reverse("core:manifest"),
            reverse("core:sw"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_nanny_cannot_open_parent_pages(self):
        self.client.force_login(self.nanny)
        self.assertEqual(self.client.get(reverse("schedule:plan")).status_code, 403)
        self.assertEqual(self.client.get(reverse("schedule:month")).status_code, 403)
        self.assertEqual(self.client.get(reverse("schedule:week")).status_code, 200)

    def test_nanny_logs_time(self):
        self.client.force_login(self.nanny)
        response = self.client.post(
            reverse("schedule:day", args=["2026-10-05"]),
            {"kind": "regular", "actual_start": "15:00", "actual_end": "19:30", "note": "Late pickup"},
        )
        self.assertRedirects(response, reverse("schedule:week_of", args=["2026-10-05"]))
        day = Day.objects.get(date=date(2026, 10, 5))
        self.assertEqual(day.updated_by, self.nanny)
        self.assertEqual(engine.balance(), 30)

    def test_nanny_cannot_mark_not_needed(self):
        self.client.force_login(self.nanny)
        response = self.client.post(reverse("schedule:day", args=["2026-10-05"]), {"kind": "not_needed"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Day.objects.exists())

    def test_day_form_shows_default_plan_without_storing_it(self):
        self.client.force_login(self.parent)
        url = reverse("schedule:day", args=["2026-10-05"])
        response = self.client.get(url)
        self.assertContains(response, 'name="planned_start" value="15:00"')
        self.assertContains(response, 'name="planned_end" value="19:00"')

        # Saving the prefilled default doesn't create a row...
        self.client.post(url, {"kind": "regular", "planned_start": "15:00", "planned_end": "19:00"})
        self.assertFalse(Day.objects.exists())
        # ...but a different plan is stored and shown.
        self.client.post(url, {"kind": "regular", "planned_start": "13:00", "planned_end": "19:00"})
        self.assertEqual(Day.objects.get().planned_start, time(13))
        self.assertContains(self.client.get(url), 'name="planned_start" value="13:00"')

    def test_confirm_worked_as_planned(self):
        self.client.force_login(self.nanny)
        url = reverse("schedule:day", args=["2026-10-05"])
        self.assertContains(self.client.get(url), "Worked as planned")
        self.client.post(url, {"confirm": "1"})
        day = Day.objects.get()
        self.assertEqual((day.actual_start, day.actual_end), (time(15), time(19)))
        self.assertNotContains(self.client.get(url), "Worked as planned (")
        self.assertEqual(engine.balance(), 0)

    def test_cannot_confirm_future_day(self):
        self.client.force_login(self.nanny)
        url = reverse("schedule:day", args=["2026-10-12"])
        self.assertNotContains(self.client.get(url), "Worked as planned (")
        self.client.post(url, {"confirm": "1"})
        self.assertFalse(Day.objects.filter(actual_start__isnull=False).exists())

    def test_extra_amount_hidden_from_nanny(self):
        Extra.objects.create(date=date(2026, 10, 5), kind="overnight", start=time(19), end=time(8))
        week = reverse("schedule:week_of", args=["2026-10-05"])
        day = reverse("schedule:day", args=["2026-10-05"])

        self.client.force_login(self.nanny)
        for url in (week, day):
            response = self.client.get(url)
            self.assertNotContains(response, "175,00 €")
            self.assertContains(response, "paid")

        self.client.force_login(self.parent)
        for url in (week, day):
            self.assertContains(self.client.get(url), "175,00 €")

    def test_money_hidden_from_nanny_on_extra_form_and_trips(self):
        Trip.objects.create(date=date(2026, 10, 5), purpose="Swimming", km=Decimal("10"))
        extra_form = reverse("schedule:extra_new") + "?date=2026-10-05"
        trips = reverse("schedule:trips_month", args=[2026, 10])

        self.client.force_login(self.nanny)
        self.assertNotContains(self.client.get(extra_form), "flat rate")
        self.assertNotContains(self.client.get(trips), "3,00 €")

        self.client.force_login(self.parent)
        self.assertContains(self.client.get(extra_form), "flat rate")
        self.assertContains(self.client.get(trips), "3,00 €")

    def test_week_shows_planned_hours(self):
        Day.objects.create(date=date(2026, 10, 13), planned_start=time(13), planned_end=time(19))
        Day.objects.create(date=date(2026, 10, 14), kind=Day.Kind.NOT_NEEDED)
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:week_of", args=["2026-10-12"]))
        self.assertContains(response, "15:00–19:00 <span class=\"muted\">· 4:00 h</span>")
        self.assertContains(response, "13:00–19:00 <span class=\"muted\">· 6:00 h</span>")
        # Mon 4 + Tue 6 + Wed 0 (not needed) + Thu 4 + Fri 4
        self.assertContains(response, "planned 18:00 h")

    def test_clearing_a_day_removes_the_row(self):
        Day.objects.create(date=date(2026, 10, 5), note="x")
        self.client.force_login(self.parent)
        self.client.post(reverse("schedule:day", args=["2026-10-05"]), {"kind": "regular"})
        self.assertFalse(Day.objects.exists())

    def test_vacation_request_and_approval(self):
        self.client.force_login(self.nanny)
        self.client.post(reverse("schedule:vacation"), {"start_date": "2026-11-02", "end_date": "2026-11-06"})
        vac = Vacation.objects.get()
        self.assertEqual(vac.status, Vacation.Status.PENDING)
        self.assertEqual(mail.outbox[-1].to, [self.parent.email])

        # The nanny cannot approve her own request.
        response = self.client.post(reverse("schedule:vacation_action", args=[vac.pk]), {"action": "approve"})
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.parent)
        self.client.post(reverse("schedule:vacation_action", args=[vac.pk]), {"action": "approve"})
        vac.refresh_from_db()
        self.assertEqual(vac.status, Vacation.Status.APPROVED)
        self.assertEqual(vac.decided_by, self.parent)
        self.assertEqual(mail.outbox[-1].to, [self.nanny.email])

    def test_parent_vacation_is_approved_directly(self):
        self.client.force_login(self.parent)
        self.client.post(reverse("schedule:vacation"), {"start_date": "2026-11-02", "end_date": "2026-11-02"})
        self.assertEqual(Vacation.objects.get().status, Vacation.Status.APPROVED)

    def test_bulk_not_needed_only_touches_workdays(self):
        self.client.force_login(self.parent)
        self.client.post(
            reverse("schedule:plan"),
            {"bulk": "1", "start": "2026-10-12", "end": "2026-10-18", "action": "not_needed", "note": "Family trip"},
        )
        self.assertEqual(Day.objects.filter(kind=Day.Kind.NOT_NEEDED).count(), 5)

    def test_bulk_reset_keeps_logged_times(self):
        Day.objects.create(date=date(2026, 10, 5), kind="not_needed", actual_start=time(15), actual_end=time(16))
        Day.objects.create(date=date(2026, 10, 6), kind="not_needed")
        self.client.force_login(self.parent)
        self.client.post(
            reverse("schedule:plan"),
            {"bulk": "1", "start": "2026-10-05", "end": "2026-10-06", "action": "reset"},
        )
        self.assertEqual(list(Day.objects.values_list("date", "kind")), [(date(2026, 10, 5), "regular")])

    def test_parent_adds_holiday(self):
        self.client.force_login(self.parent)
        self.client.post(reverse("schedule:plan"), {"holiday": "1", "date": "2026-12-25", "name": "Christmas Day"})
        self.assertTrue(Holiday.objects.filter(date=date(2026, 12, 25)).exists())

    def test_payout_from_month_page(self):
        self.client.force_login(self.parent)
        self.client.post(
            reverse("schedule:month_of", args=[2026, 10]),
            {"date": "2026-10-07", "kind": "payout", "hours": "2", "note": ""},
        )
        adj = Adjustment.objects.get()
        self.assertEqual((adj.minutes, adj.paid_out), (-120, True))

    def test_trip_crud(self):
        self.client.force_login(self.nanny)
        self.client.post(
            reverse("schedule:trips"), {"date": "2026-10-05", "purpose": "Swimming", "km": "8.4"}
        )
        trip = Trip.objects.get()
        self.assertEqual(trip.updated_by, self.nanny)
        self.client.post(reverse("schedule:trip", args=[trip.pk]), {"delete": "1"})
        self.assertFalse(Trip.objects.exists())
