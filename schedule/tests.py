from datetime import date, time
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
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

    def test_month_planned_includes_default_and_future_days(self):
        # October 2026 from the 1st: 22 workdays × 4 h; one day planned 2 h longer, one not needed.
        Day.objects.create(date=date(2026, 10, 20), planned_start=time(13), planned_end=time(19))
        Day.objects.create(date=date(2026, 10, 21), kind=Day.Kind.NOT_NEEDED)
        s = engine.month_summary(2026, 10, TODAY)
        self.assertEqual(s.planned, 22 * 240 + 120 - 240)
        # Contract hours and worked only cover the days before today (1, 2, 5, 6, 7 Oct).
        self.assertEqual(s.target, 5 * 240)
        self.assertEqual(s.worked, 5 * 240)
        self.assertTrue(s.is_partial)
        self.assertFalse(engine.month_summary(2026, 10, date(2026, 11, 2)).is_partial)

    def test_month_shows_unconfirmed_hours(self):
        # Oct 1, 2, 5, 6, 7 are past; 1 and 5 are confirmed, 6 worked longer, 2 and 7 not entered.
        Day.objects.create(date=date(2026, 10, 1), actual_start=time(15), actual_end=time(19))
        Day.objects.create(date=date(2026, 10, 5), actual_start=time(15), actual_end=time(19))
        Day.objects.create(date=date(2026, 10, 6), actual_start=time(15), actual_end=time(20))
        s = engine.month_summary(2026, 10, TODAY)
        self.assertEqual([d.date.day for d in s.unconfirmed_days], [2, 7])
        self.assertEqual(s.unconfirmed, 2 * 240)
        self.assertEqual(s.worked, 4 * 240 + 300)  # unconfirmed days are included

    def test_partial_month_salary(self):
        Contract.objects.all().delete()
        Contract.objects.create(start_date=date(2026, 10, 16))
        s = engine.month_summary(2026, 10, TODAY)
        self.assertEqual(s.salary, engine.money(Decimal("2166.67") * 16 / 31))

    def test_ytd_uses_entered_times_then_plan(self):
        # Past: Mon 5 worked one hour longer. Future: Mon 12 not needed, Tue 13 planned 2 h longer.
        Day.objects.create(date=date(2026, 10, 5), actual_start=time(15), actual_end=time(20))
        Day.objects.create(date=date(2026, 10, 12), kind=Day.Kind.NOT_NEEDED)
        Day.objects.create(date=date(2026, 10, 13), planned_start=time(13), planned_end=time(19))
        Extra.objects.create(date=date(2026, 10, 14), kind="evening", start=time(19), end=time(21), settlement="time_off")
        Adjustment.objects.create(date=date(2026, 10, 15), minutes=-30)

        days = engine.with_ytd(engine.compute_days(date(2026, 10, 12), date(2026, 10, 18), TODAY), TODAY)
        ytd = {d.date.day: d.ytd for d in days}
        self.assertEqual(ytd[12], 60 - 240)
        self.assertEqual(ytd[13], 60 - 240 + 120)
        self.assertEqual(ytd[14], 60 - 240 + 120 + 120)
        self.assertEqual(ytd[15], 60 - 240 + 120 + 120 - 30)
        self.assertEqual(ytd[18], ytd[15])
        # The actual time account today is not affected by future plans.
        self.assertEqual(self.balance(), 60)

    def test_ytd_restarts_on_1_january(self):
        Day.objects.create(date=date(2026, 12, 30), kind=Day.Kind.NOT_NEEDED)
        days = engine.with_ytd(engine.compute_days(date(2026, 12, 28), date(2027, 1, 3), TODAY), TODAY)
        ytd = {d.date: d.ytd for d in days}
        self.assertEqual(ytd[date(2026, 12, 31)], -240)
        self.assertEqual(ytd[date(2027, 1, 1)], 0)

    def test_badges_say_what_a_day_was_once_it_happened(self):
        Day.objects.create(date=date(2026, 10, 2), kind=Day.Kind.NOT_NEEDED)
        Day.objects.create(date=date(2026, 10, 5), kind=Day.Kind.SICK)
        Vacation.objects.create(start_date=date(2026, 10, 6), end_date=date(2026, 10, 6), status="approved")
        Day.objects.create(date=TODAY, actual_start=time(15), actual_end=time(19))
        Day.objects.create(date=date(2026, 10, 12), kind=Day.Kind.NOT_NEEDED)
        labels = {d.date.day: d.label for d in engine.compute_days(START, date(2026, 10, 13), TODAY)}
        self.assertEqual(labels[1], "To confirm")      # past, nothing entered (still counts as planned)
        self.assertEqual(labels[2], "Was free")
        self.assertEqual(labels[3], "Day off")         # weekend
        self.assertEqual(labels[5], "Was sick")
        self.assertEqual(labels[6], "Was on vacation")
        self.assertEqual(labels[8], "Worked")          # today, times entered
        self.assertEqual(labels[9], "Scheduled")       # tomorrow
        self.assertEqual(labels[12], "No care needed") # future
        # Today without entries is still scheduled; a past day becomes "Worked" once confirmed.
        Day.objects.filter(date=TODAY).delete()
        self.assertEqual(engine.compute_days(TODAY, TODAY, TODAY)[0].label, "Scheduled")
        before = self.balance()
        Day.objects.create(date=START, actual_start=time(15), actual_end=time(19))
        self.assertEqual(engine.compute_days(START, START, TODAY)[0].label, "Worked")
        self.assertEqual(self.balance(), before)  # confirming as planned doesn't change the hours

    def test_duration_format(self):
        from .templatetags.nani import hm, hm_signed

        self.assertEqual([hm(240), hm(90), hm(5), hm(0), hm(-60), hm(1230)],
                         ["4h", "1h30m", "0h05m", "0h", "−1h", "20h30m"])
        self.assertEqual([hm_signed(90), hm_signed(-30), hm_signed(0)], ["+1h30m", "−0h30m", "+0h"])

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
            reverse("schedule:home"),
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

    def test_nanny_home_lists_days_to_confirm(self):
        Day.objects.create(date=date(2026, 10, 1), actual_start=time(15), actual_end=time(19))
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:home"))
        self.assertEqual([d.date.day for d in response.context["to_confirm"]], [2, 5, 6, 7])
        self.assertContains(response, "Days to confirm")
        self.assertContains(response, "Confirm all as planned")
        # Parents don't get the card.
        self.client.force_login(self.parent)
        self.assertNotContains(self.client.get(reverse("schedule:home")), "Days to confirm")

    def test_confirm_one_and_all_from_home(self):
        self.client.force_login(self.nanny)
        response = self.client.post(reverse("schedule:day", args=["2026-10-02"]), {"next": "home", "confirm": "1"})
        self.assertRedirects(response, reverse("schedule:home"))
        self.assertEqual([d.date.day for d in engine.days_to_confirm(TODAY)], [1, 5, 6, 7])

        response = self.client.post(reverse("schedule:confirm_all"), {"next": "home"})
        self.assertRedirects(response, reverse("schedule:home"))
        self.assertEqual(engine.days_to_confirm(TODAY), [])
        self.assertEqual(Day.objects.get(date=date(2026, 10, 7)).actual_end, time(19))
        self.assertNotContains(self.client.get(reverse("schedule:home")), "Days to confirm")
        # Today and future days are never confirmed in bulk.
        self.assertFalse(Day.objects.filter(date__gte=TODAY).exists())

    def test_month_page_lists_unconfirmed_days(self):
        self.client.force_login(self.parent)
        response = self.client.get(reverse("schedule:month_of", args=[2026, 10]))
        self.assertContains(response, "of which not confirmed yet")
        self.assertContains(response, reverse("schedule:day", args=["2026-10-07"]))
        self.assertContains(response, "count as worked as planned until they are confirmed")
        # Nothing to confirm: no row.
        for day in (1, 2, 5, 6, 7):
            Day.objects.create(date=date(2026, 10, day), actual_start=time(15), actual_end=time(19))
        self.assertNotContains(self.client.get(reverse("schedule:month_of", args=[2026, 10])), "of which not confirmed yet")

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

    def test_day_card_shows_as_planned_or_the_plan(self):
        Day.objects.create(date=date(2026, 10, 5), actual_start=time(15), actual_end=time(19))
        Day.objects.create(date=date(2026, 10, 6), actual_start=time(15), actual_end=time(18))
        self.client.force_login(self.nanny)
        html = self.client.get(reverse("schedule:week_of", args=["2026-10-05"])).content.decode()
        monday = html.split('<span class="dn">5</span>')[1].split("</li>")[0]
        tuesday = html.split('<span class="dn">6</span>')[1].split("</li>")[0]
        self.assertIn("as planned", monday)
        self.assertNotIn("Planned 15:00", monday)
        self.assertIn("Planned 15:00–19:00 · 4h", tuesday)
        self.assertNotIn("as planned", tuesday)

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
        self.assertContains(response, "15:00–19:00 <span class=\"muted\">· 4h</span>")
        self.assertContains(response, "13:00–19:00 <span class=\"muted\">· 6h</span>")
        # Mon 4 + Tue 6 + Wed 0 (not needed) + Thu 4 + Fri 4
        self.assertContains(response, "Planned 18h")

    def test_nanny_home_shows_today_with_actions(self):
        self.nanny.name = "Anna Example"
        self.nanny.save()
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:home"))
        self.assertContains(response, ", Anna</h2>")
        self.assertContains(response, "Today, Thursday 8 October")
        self.assertContains(response, "Worked as planned (15:00–19:00)")
        self.assertContains(response, "Enter different times")
        self.assertContains(response, "Purpose, e.g. swimming")
        self.assertContains(response, "Time account")

    def test_parent_home_shows_today_without_actions(self):
        Trip.objects.create(date=TODAY, purpose="Swimming", km=Decimal("5"))
        self.client.force_login(self.parent)
        response = self.client.get(reverse("schedule:home"))
        self.assertContains(response, "Today, Thursday 8 October")
        self.assertContains(response, "Time account")
        self.assertContains(response, "Swimming")
        self.assertNotContains(response, "Worked as planned (")
        self.assertNotContains(response, "Enter different times")
        self.assertNotContains(response, "Purpose, e.g. swimming")

    def test_week_has_swipe_targets(self):
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:week_of", args=["2026-10-07"]))
        self.assertContains(response, f'id="week-prev" href="{reverse("schedule:week_of", args=["2026-09-28"])}"')
        self.assertContains(response, f'id="week-next" href="{reverse("schedule:week_of", args=["2026-10-12"])}"')
        self.assertContains(response, "/static/js/app.js")

    def test_weeks_page_has_no_today_card_or_time_account(self):
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:week"))
        self.assertNotContains(response, "today-card")
        self.assertNotContains(response, 'class="balance card"')

    def test_today_card_forms_return_home(self):
        self.client.force_login(self.nanny)
        today = TODAY.isoformat()
        response = self.client.post(
            reverse("schedule:day", args=[today]),
            {"next": "home", "kind": "regular", "break_minutes": "", "actual_start": "15:00", "actual_end": "19:30", "note": ""},
        )
        self.assertRedirects(response, reverse("schedule:home"))
        self.assertEqual(Day.objects.get(date=TODAY).actual_end, time(19, 30))

        response = self.client.post(
            reverse("schedule:trips"), {"next": "home", "date": today, "purpose": "Swimming", "km": "6.5"}
        )
        self.assertRedirects(response, reverse("schedule:home"))
        self.assertContains(self.client.get(reverse("schedule:home")), "Swimming")

    def test_today_card_confirm_returns_home(self):
        self.client.force_login(self.nanny)
        response = self.client.post(reverse("schedule:day", args=[TODAY.isoformat()]), {"next": "home", "confirm": "1"})
        self.assertRedirects(response, reverse("schedule:home"))
        self.assertTrue(Day.objects.get(date=TODAY).actual_start)

    # --- improvements taken from the chef app ---

    def test_delete_buttons_ask_first(self):
        extra = Extra.objects.create(date=date(2026, 10, 5), kind="evening", start=time(19), end=time(22))
        trip = Trip.objects.create(date=date(2026, 10, 5), purpose="Swimming", km=Decimal("5"))
        Holiday.objects.create(date=date(2026, 12, 25), name="Christmas Day")
        Vacation.objects.create(start_date=date(2026, 11, 2), end_date=date(2026, 11, 2), status="approved")
        Adjustment.objects.create(date=date(2026, 10, 5), minutes=-60, paid_out=True)
        self.client.force_login(self.parent)
        for url in [
            reverse("schedule:extra", args=[extra.pk]),
            reverse("schedule:trip", args=[trip.pk]),
            reverse("schedule:plan"),
            reverse("schedule:vacation") + "?year=2026",
            reverse("schedule:month_of", args=[2026, 10]),
        ]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "data-confirm=")

    def test_app_script_registers_service_worker_and_brand_icon(self):
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:home"))
        self.assertContains(response, f'data-sw-url="{reverse("core:sw")}"')
        self.assertContains(response, 'class="brand"><img src="/static/icons/icon-180.png"')
        self.assertNotContains(response, "navigator.serviceWorker")

    def test_selected_week_is_remembered_for_the_day(self):
        self.client.force_login(self.nanny)
        self.client.get(reverse("schedule:week_of", args=["2026-10-21"]))
        self.assertEqual(self.client.get(reverse("schedule:week")).context["start"], date(2026, 10, 19))
        # "Back to this week" links to the current week explicitly.
        self.assertContains(self.client.get(reverse("schedule:week")), reverse("schedule:week_of", args=["2026-10-05"]))
        # The next day the current week is shown again.
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 10, 9)):
            self.assertEqual(self.client.get(reverse("schedule:week")).context["start"], date(2026, 10, 5))

    def test_home_shows_rest_of_the_week(self):
        self.client.force_login(self.nanny)
        response = self.client.get(reverse("schedule:home"))  # Thursday
        self.assertContains(response, "Rest of the week")
        self.assertEqual([d.date.day for d in response.context["coming"]], [9, 10, 11])

    def test_home_looks_ahead_from_friday(self):
        Day.objects.create(date=date(2026, 10, 14), kind=Day.Kind.NOT_NEEDED)
        self.client.force_login(self.nanny)
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 10, 9)):
            response = self.client.get(reverse("schedule:home"))
        self.assertContains(response, "Next week")
        self.assertEqual([d.date.day for d in response.context["coming"]], [12, 13, 14, 15, 16])
        self.assertContains(response, "No care needed")

    def test_nanny_is_emailed_when_parents_change_her_plan(self):
        self.client.force_login(self.parent)
        mail.outbox.clear()
        self.client.post(
            reverse("schedule:plan"),
            {"bulk": "1", "start": "2026-10-12", "end": "2026-10-13", "action": "not_needed", "note": ""},
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.nanny.email])
        self.assertIn("Mon 12 Oct: no care needed (was 15:00–19:00)", message.body)
        self.assertIn("Tue 13 Oct: no care needed (was 15:00–19:00)", message.body)
        self.assertIn("/week/2026-10-12/", message.body)

        mail.outbox.clear()
        self.client.post(reverse("schedule:day", args=["2026-10-15"]),
                         {"kind": "regular", "planned_start": "13:00", "planned_end": "19:00"})
        self.assertIn("Thu 15 Oct: 13:00–19:00 (was 15:00–19:00)", mail.outbox[0].body)

        mail.outbox.clear()
        self.client.post(reverse("schedule:plan"), {"holiday": "1", "date": "2026-10-16", "name": "Test day"})
        self.assertIn("Fri 16 Oct: holiday (Test day) (was 15:00–19:00)", mail.outbox[0].body)

    def test_no_plan_email_for_past_days_unchanged_days_or_nanny_edits(self):
        self.client.force_login(self.parent)
        mail.outbox.clear()
        # Past day, and a note only (the plan looks the same).
        self.client.post(reverse("schedule:day", args=["2026-10-05"]), {"kind": "not_needed"})
        self.client.post(reverse("schedule:day", args=["2026-10-12"]), {"kind": "regular", "note": "Pick up at 15:00"})
        self.assertEqual(len(mail.outbox), 0)
        # The nanny's own entries don't email her.
        self.client.force_login(self.nanny)
        self.client.post(reverse("schedule:day", args=["2026-10-12"]), {"kind": "sick"})
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PLAN_EMAILS=False)
    def test_plan_emails_can_be_switched_off(self):
        self.client.force_login(self.parent)
        mail.outbox.clear()
        self.client.post(reverse("schedule:day", args=["2026-10-12"]), {"kind": "not_needed"})
        self.assertEqual(len(mail.outbox), 0)

    # --- admin screen ---

    def test_admin_screen_is_for_parents_only(self):
        self.client.force_login(self.nanny)
        for name in ("manage", "person_new", "contract_new"):
            self.assertEqual(self.client.get(reverse(f"schedule:{name}")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("schedule:home")), reverse("schedule:manage"))
        self.client.force_login(self.parent)
        response = self.client.get(reverse("schedule:manage"))
        self.assertContains(response, "nanny@example.com")
        self.assertContains(response, "Mon, Tue, Wed, Thu, Fri · 15:00–19:00 (20h/week)")

    def test_add_and_edit_person(self):
        self.client.force_login(self.parent)
        response = self.client.post(
            reverse("schedule:person_new"),
            {"name": "Laura Example", "email": "Laura@Example.com", "role": "parent", "is_active": "on"},
        )
        self.assertRedirects(response, reverse("schedule:manage"))
        laura = User.objects.get(email="laura@example.com")
        self.assertFalse(laura.has_usable_password())
        self.assertFalse(laura.is_staff)

        # Duplicate addresses are refused, whatever the case.
        response = self.client.post(
            reverse("schedule:person_new"), {"name": "X", "email": "NANNY@example.com", "role": "nanny", "is_active": "on"}
        )
        self.assertContains(response, "already uses this email")

        # Deactivating instead of deleting.
        self.client.post(reverse("schedule:person", args=[self.nanny.pk]),
                         {"name": "", "email": self.nanny.email, "role": "nanny"})
        self.nanny.refresh_from_db()
        self.assertFalse(self.nanny.is_active)

    def test_cannot_lock_yourself_out_or_remove_last_parent(self):
        self.client.force_login(self.parent)
        # Own role and active flag can't be changed (disabled fields keep their value).
        self.client.post(reverse("schedule:person", args=[self.parent.pk]),
                         {"name": "Me", "email": self.parent.email, "role": "nanny"})
        self.parent.refresh_from_db()
        self.assertEqual((self.parent.role, self.parent.is_active, self.parent.name), ("parent", True, "Me"))

        # With two parents, one can deactivate the other.
        other = User.objects.create_user(email="other@example.com", role="parent")
        self.client.force_login(other)
        self.client.post(reverse("schedule:person", args=[self.parent.pk]), {"email": self.parent.email, "role": "parent"})
        self.parent.refresh_from_db()
        self.assertFalse(self.parent.is_active)

        # But the last active parent can't be removed, e.g. by a technical admin who isn't a parent.
        tech = User.objects.create_superuser(email="tech@example.com", role="nanny")
        self.client.force_login(tech)
        response = self.client.post(reverse("schedule:person", args=[other.pk]), {"email": other.email, "role": "nanny", "is_active": "on"})
        self.assertContains(response, "at least one active parent")
        other.refresh_from_db()
        self.assertEqual(other.role, "parent")

    def test_admin_access_only_granted_by_superusers(self):
        self.client.force_login(self.parent)
        self.assertNotContains(self.client.get(reverse("schedule:person_new")), "admin_access")
        self.client.post(reverse("schedule:person", args=[self.nanny.pk]),
                         {"email": self.nanny.email, "role": "nanny", "is_active": "on", "admin_access": "on"})
        self.nanny.refresh_from_db()
        self.assertFalse(self.nanny.is_superuser)

        boss = User.objects.create_superuser(email="boss@example.com")
        self.client.force_login(boss)
        self.client.post(reverse("schedule:person", args=[self.parent.pk]),
                         {"email": self.parent.email, "role": "parent", "is_active": "on", "admin_access": "on"})
        self.parent.refresh_from_db()
        self.assertTrue(self.parent.is_superuser and self.parent.is_staff)

    def test_edit_contract_and_add_new_terms(self):
        contract = Contract.objects.get()
        self.client.force_login(self.parent)
        response = self.client.get(reverse("schedule:contract", args=[contract.pk]))
        self.assertContains(response, 'value="2166.67"')

        data = {
            "nanny": self.nanny.pk, "start_date": "2026-10-01", "end_date": "", "days": ["0", "1", "2", "3"],
            "default_start": "14:00", "default_end": "19:00", "monthly_salary": "2166.67", "hourly_rate": "25.00",
            "night_flat_rate": "50.00", "night_from": "22:00", "night_until": "06:00", "km_rate": "0.30",
            "vacation_days_per_year": "25",
        }
        self.assertRedirects(self.client.post(reverse("schedule:contract", args=[contract.pk]), data), reverse("schedule:manage"))
        contract.refresh_from_db()
        self.assertEqual((contract.workdays, contract.default_start, contract.nanny), ("0,1,2,3", time(14), self.nanny))

        # New terms start pre-filled with the current ones; a raise from November.
        self.assertContains(self.client.get(reverse("schedule:contract_new")), 'value="14:00"')
        self.client.post(reverse("schedule:contract_new"), {**data, "start_date": "2026-11-01", "hourly_rate": "27.00"})
        self.assertEqual(Contract.objects.count(), 2)
        self.assertEqual(engine.month_summary(2026, 11, TODAY).days[0].contract.hourly_rate, Decimal("27.00"))
        # Two terms can't start on the same day.
        response = self.client.post(reverse("schedule:contract_new"), {**data, "start_date": "2026-11-01"})
        self.assertContains(response, "already terms starting on this day")

    def test_contract_needs_a_working_day_and_last_one_stays(self):
        contract = Contract.objects.get()
        self.client.force_login(self.parent)
        response = self.client.post(reverse("schedule:contract", args=[contract.pk]), {"start_date": "2026-10-01"})
        self.assertContains(response, "This field is required")
        self.client.post(reverse("schedule:contract", args=[contract.pk]), {"delete": "1"})
        self.assertTrue(Contract.objects.exists())

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
