from datetime import date
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    PreviousShiftRecord,
    ShiftAssignment,
    ShiftPeriod,
    ShiftRequest,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)
from .services.generator import (
    MIN_PUBLIC_HOLIDAYS_PER_WEEK,
    build_required_staff_warnings,
    generate_monthly_shift,
    previous_weekly_public_holiday_counts,
    week_ranges_in_month,
    week_start,
)
from .services.importers import build_template_workbook, import_master_workbook


class SampleFlowTests(TestCase):
    def test_sample_workbook_imports_thirty_staff_and_generates_shift(self):
        workbook = build_template_workbook(sample=True)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)

        result = import_master_workbook(stream)
        period = generate_monthly_shift(date(2026, 7, 1))

        self.assertEqual(result["staff"], 30)
        self.assertEqual(result["works"], 8)
        self.assertEqual(
            workbook.sheetnames,
            ["スキル表", "業務マスタ", "スキル区分", "先月シフト実績", "シフト提出"],
        )
        self.assertGreater(result["requests"], 0)
        self.assertEqual(
            [workbook["スキル表"].cell(row=1, column=column).value for column in range(1, 5)],
            ["社員番号", "氏名", "公休数", "A"],
        )
        self.assertNotIn(
            "備考",
            [workbook["スキル表"].cell(row=1, column=column).value for column in range(1, workbook["スキル表"].max_column + 1)],
        )
        self.assertEqual(Staff.objects.count(), 30)
        self.assertEqual(WorkType.objects.count(), 8)
        self.assertEqual(ShiftAssignment.objects.filter(period=period).count(), 930)
        self.assertFalse(
            period.warnings.filter(message__contains="必要人数を満たせません").exists()
        )
        self.assertFalse(build_required_staff_warnings(period))

    def test_import_reads_shift_requests_from_excel(self):
        workbook = build_template_workbook(sample=False)
        sheet = workbook["シフト提出"]
        sheet.cell(row=1, column=3).value = "2026/7/1"
        sheet.cell(row=1, column=4).value = "2026/7/2"
        sheet.cell(row=1, column=5).value = "2026/7/3"
        for column in range(3, sheet.max_column + 1):
            sheet.cell(row=2, column=column).value = ""
        sheet.cell(row=2, column=3).value = "公休"
        sheet.cell(row=2, column=4).value = "有休"
        sheet.cell(row=2, column=5).value = "不可"
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)

        result = import_master_workbook(stream)
        staff = Staff.objects.get(employee_number="S001")

        self.assertEqual(result["requests"], 3)
        self.assertEqual(
            ShiftRequest.objects.get(staff=staff, day=date(2026, 7, 1)).kind,
            ShiftRequest.Kind.PUBLIC_HOLIDAY,
        )
        self.assertEqual(
            ShiftRequest.objects.get(staff=staff, day=date(2026, 7, 2)).kind,
            ShiftRequest.Kind.PAID_LEAVE,
        )
        self.assertEqual(
            ShiftRequest.objects.get(staff=staff, day=date(2026, 7, 3)).kind,
            ShiftRequest.Kind.UNAVAILABLE,
        )

    def test_generation_uses_shift_requests_as_first_pass_guidance(self):
        work = WorkType.objects.create(name="A", required_staff_per_day=1, active=True)
        level = SkillLevel.objects.create(symbol="○", label="配置可", assignable=True)
        staff_a = Staff.objects.create(employee_number="S001", name="有休 太郎")
        staff_b = Staff.objects.create(employee_number="S002", name="公休 花子")
        staff_c = Staff.objects.create(employee_number="S003", name="勤務 次郎")
        for staff in (staff_a, staff_b, staff_c):
            StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        ShiftRequest.objects.create(
            staff=staff_a,
            day=date(2026, 7, 1),
            kind=ShiftRequest.Kind.PAID_LEAVE,
            raw_value="有休",
        )
        ShiftRequest.objects.create(
            staff=staff_b,
            day=date(2026, 7, 1),
            kind=ShiftRequest.Kind.PUBLIC_HOLIDAY,
            raw_value="公休",
        )

        period = generate_monthly_shift(date(2026, 7, 1))

        self.assertEqual(
            ShiftAssignment.objects.get(period=period, staff=staff_a, day=date(2026, 7, 1)).status,
            ShiftAssignment.Status.PAID_LEAVE,
        )
        self.assertEqual(
            ShiftAssignment.objects.get(period=period, staff=staff_b, day=date(2026, 7, 1)).status,
            ShiftAssignment.Status.PUBLIC_HOLIDAY,
        )
        self.assertEqual(
            ShiftAssignment.objects.get(period=period, staff=staff_c, day=date(2026, 7, 1)).status,
            ShiftAssignment.Status.WORK,
        )

    def test_previous_month_records_are_counted_for_cross_month_week(self):
        staff = Staff.objects.create(employee_number="S001", name="月跨ぎ 太郎")
        PreviousShiftRecord.objects.create(
            staff=staff,
            day=date(2026, 6, 28),
            status=PreviousShiftRecord.Status.PUBLIC_HOLIDAY,
            raw_value="公休",
        )
        PreviousShiftRecord.objects.create(
            staff=staff,
            day=date(2026, 6, 29),
            status=PreviousShiftRecord.Status.PUBLIC_HOLIDAY,
            raw_value="公休",
        )

        counts = previous_weekly_public_holiday_counts([staff], date(2026, 7, 1))

        self.assertEqual(counts[(week_start(date(2026, 7, 1)), staff.id)], 2)


class ManagementScreenTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="pass")
        self.client.login(username="admin", password="pass")

    def test_work_manage_can_create_work(self):
        response = self.client.post(
            reverse("work_manage"),
            {
                "name": "A",
                "required_staff_per_day": "3",
                "color": "#2563eb",
                "display_order": "1",
                "active": "on",
            },
        )

        self.assertRedirects(response, reverse("work_manage"))
        self.assertTrue(WorkType.objects.filter(name="A", required_staff_per_day=3).exists())
        self.assertContains(self.client.get(reverse("work_manage")), "業務管理")

    def test_work_can_be_destroyed(self):
        staff = Staff.objects.create(employee_number="S001", name="山田 太郎")
        work = WorkType.objects.create(name="A", required_staff_per_day=1, active=True)
        level = SkillLevel.objects.create(symbol="○", label="配置可", assignable=True)
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        period = ShiftPeriod.objects.create(month=date(2026, 7, 1))
        assignment = ShiftAssignment.objects.create(
            period=period,
            staff=staff,
            day=date(2026, 7, 1),
            status=ShiftAssignment.Status.WORK,
            work_type=work,
        )

        response = self.client.get(reverse("work_destroy", args=[work.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "業務を削除")

        response = self.client.post(reverse("work_destroy", args=[work.pk]))

        self.assertRedirects(response, reverse("work_manage"))
        self.assertFalse(WorkType.objects.filter(pk=work.pk).exists())
        self.assertFalse(StaffSkill.objects.filter(staff=staff).exists())
        assignment.refresh_from_db()
        self.assertIsNone(assignment.work_type)

    def test_staff_manage_can_create_edit_and_delete_all_staff(self):
        response = self.client.post(
            reverse("staff_manage"),
            {
                "employee_number": "S001",
                "name": "山田 太郎",
                "public_holiday_count": "9",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("staff_manage"))
        staff = Staff.objects.get(employee_number="S001")
        self.assertEqual(staff.name, "山田 太郎")
        self.assertEqual(staff.public_holiday_count, 9)
        self.assertContains(self.client.get(reverse("staff_manage")), "スタッフ管理")

        response = self.client.post(
            reverse("staff_edit", args=[staff.pk]),
            {
                "employee_number": "S001",
                "name": "山田 花子",
                "public_holiday_count": "8",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("staff_manage"))
        staff.refresh_from_db()
        self.assertEqual(staff.name, "山田 花子")
        self.assertEqual(staff.public_holiday_count, 8)

        response = self.client.post(reverse("staff_delete_all"))

        self.assertRedirects(response, reverse("staff_manage"))
        self.assertEqual(Staff.objects.count(), 0)

    def test_skill_map_can_update_skill(self):
        staff = Staff.objects.create(employee_number="S001", name="山田 太郎")
        work = WorkType.objects.create(name="A", required_staff_per_day=1, active=True)
        level = SkillLevel.objects.create(symbol="○", label="配置可", assignable=True)

        response = self.client.post(
            reverse("skill_map"),
            {f"skill_{staff.id}_{work.id}": str(level.id)},
        )

        self.assertRedirects(response, reverse("skill_map"))
        self.assertTrue(
            StaffSkill.objects.filter(staff=staff, work_type=work, level=level).exists()
        )
        self.assertContains(self.client.get(reverse("skill_map")), "スタッフスキル")

    def test_shift_detail_restores_monthly_table_layout(self):
        workbook = build_template_workbook(sample=True)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        import_master_workbook(stream)
        period = generate_monthly_shift(date(2026, 7, 1))

        response = self.client.get(reverse("shift_detail", args=[period.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "monthly-table")
        self.assertContains(response, "シフト編集モード")
        self.assertContains(response, "出勤")
        self.assertContains(response, "公休")
        self.assertContains(response, "有給")
        self.assertContains(response, "必要")
        self.assertContains(response, "daily-work-stat-row")

    def test_shift_detail_shows_holiday_name_above_weekday(self):
        workbook = build_template_workbook(sample=True)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        import_master_workbook(stream)
        period = generate_monthly_shift(date(2026, 7, 1))

        with patch(
            "shift_core.services.exporters._holidays_for_month",
            return_value={date(2026, 7, 20): "海の日"},
        ):
            response = self.client.get(reverse("shift_detail", args=[period.pk]))

        self.assertEqual(response.status_code, 200)
        holiday_day = next(day for day in response.context["days"] if day["number"] == 20)
        self.assertEqual(holiday_day["holiday"], "海の日")
        self.assertTrue(holiday_day["is_non_workday"])
        self.assertContains(response, '<small class="holiday-name">海の日</small>', html=True)
