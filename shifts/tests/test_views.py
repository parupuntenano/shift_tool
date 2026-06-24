from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from shifts.infrastructure.models import (
    Company,
    CompanyMembership,
    ConstraintType,
    IndividualConstraint,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)


class LoginRoutingTests(TestCase):
    def test_admin_is_routed_to_manager_dashboard(self):
        company = Company.objects.create(name="テスト", code="test")
        user = get_user_model().objects.create_user("admin-test", password="pass")
        CompanyMembership.objects.create(company=company, user=user, role="admin")
        self.client.login(username="admin-test", password="pass")
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("manager_dashboard"))


class StaffPasswordChangeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="テスト", code="password-test")
        self.user = get_user_model().objects.create_user("S001", password="0000")
        CompanyMembership.objects.create(
            company=self.company, user=self.user, role="staff"
        )
        Staff.objects.create(
            company=self.company, user=self.user, employee_number="S001", name="青木"
        )
        self.client.login(username="S001", password="0000")

    def test_staff_can_change_own_password(self):
        response = self.client.post(
            reverse("staff_change_password"),
            {
                "old_password": "0000",
                "new_password1": "new-secure-password-123",
                "new_password2": "new-secure-password-123",
            },
        )
        self.assertRedirects(response, reverse("staff_change_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-password-123"))
        self.assertTrue(response.wsgi_request.user.is_authenticated)


class ManagerCrudTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="テスト", code="crud-test")
        self.other_company = Company.objects.create(name="他社", code="other")
        self.user = get_user_model().objects.create_user("crud-admin", password="pass")
        CompanyMembership.objects.create(
            company=self.company, user=self.user, role="admin"
        )
        self.client.login(username="crud-admin", password="pass")

    def test_can_edit_work_in_own_company(self):
        work = WorkType.objects.create(
            company=self.company, name="旧業務", display_order=1
        )
        response = self.client.post(
            reverse("work_edit", args=[work.pk]),
            {
                "name": "新業務",
                "display_order": 2,
                "required_staff_per_day": 3,
                "active": "on",
            },
        )
        self.assertRedirects(response, reverse("work_manage"))
        work.refresh_from_db()
        self.assertEqual((work.name, work.required_staff_per_day), ("新業務", 3))

    def test_cannot_edit_other_company_data(self):
        work = WorkType.objects.create(company=self.other_company, name="他社業務")
        response = self.client.get(reverse("work_edit", args=[work.pk]))
        self.assertEqual(response.status_code, 404)

    def test_can_download_import_template(self):
        response = self.client.get(reverse("download_import_template"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(
            "shift_import_template.xlsx",
            response["Content-Disposition"],
        )

        workbook = load_workbook(BytesIO(response.content), read_only=True)
        sheet = workbook["スキル表"]
        levels = workbook["スキル区分"]
        works = workbook["業務マスタ"]
        guide = workbook["入力ルール"]

        self.assertEqual(
            [sheet.cell(row=1, column=index).value for index in range(1, 4)],
            ["社員番号", "氏名", "備考"],
        )
        self.assertIsNone(sheet["D1"].value)
        self.assertEqual(sheet["A2"].value, "S001")
        self.assertEqual(
            [levels.cell(row=1, column=index).value for index in range(1, 5)],
            ["記号", "意味", "優先度", "アサイン可"],
        )
        self.assertIsNone(levels["E1"].value)
        self.assertEqual(levels["A2"].value, "◎")
        self.assertEqual(
            [works.cell(row=1, column=index).value for index in range(1, 4)],
            ["業務名", "最低必要人数", "有効"],
        )
        self.assertEqual(works["B2"].value, 1)
        self.assertEqual(guide["A1"].value, "項目")

    def test_can_download_import_sample(self):
        response = self.client.get(reverse("download_import_sample"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(
            "shift_import_sample.xlsx",
            response["Content-Disposition"],
        )

        workbook = load_workbook(BytesIO(response.content), read_only=True)
        sheet = workbook["スキル表"]
        works = workbook["業務マスタ"]
        levels = workbook["スキル区分"]

        self.assertEqual(sheet.max_row, 11)
        self.assertEqual(
            [sheet.cell(row=1, column=index).value for index in range(1, 7)],
            ["社員番号", "氏名", "備考", "受付", "ロール", "エーカス"],
        )
        self.assertEqual(sheet["A2"].value, "S001")
        self.assertEqual(sheet["D2"].value, "◎")
        self.assertEqual(works.max_row, 4)
        self.assertEqual(works["A2"].value, "受付")
        self.assertEqual(works["B2"].value, 2)
        self.assertEqual(levels["A2"].value, "◎")

    def test_can_delete_constraint_after_confirmation(self):
        rule = IndividualConstraint.objects.create(
            company=self.company, name="テスト条件", kind="custom"
        )
        response = self.client.post(reverse("constraint_delete", args=[rule.pk]))
        self.assertRedirects(response, reverse("constraint_manage"))
        self.assertFalse(IndividualConstraint.objects.filter(pk=rule.pk).exists())

    def test_used_skill_level_is_protected_from_delete(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S1", name="A"
        )
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(
            company=self.company, symbol="○", meaning="可"
        )
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        response = self.client.post(reverse("skill_delete", args=[level.pk]))
        self.assertRedirects(response, reverse("skill_manage"))
        self.assertTrue(SkillLevel.objects.filter(pk=level.pk).exists())

    def test_skill_map_search_filters_by_staff_and_work(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S100", name="検索 太郎"
        )
        work = WorkType.objects.create(company=self.company, name="検索対象業務")
        level = SkillLevel.objects.create(
            company=self.company, symbol="◎", meaning="リーダー"
        )
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        response = self.client.get(reverse("skill_map"), {"delete_q": "検索対象"})
        self.assertContains(response, "検索 太郎")
        self.assertContains(response, "検索対象業務")
        response = self.client.get(reverse("skill_map"), {"delete_q": "存在しない"})
        self.assertContains(response, "条件に一致するスキル設定はありません。")
        self.assertNotContains(response, 'class="skill-checkbox"')

    def test_skill_map_staff_search_filters_edit_matrix(self):
        target = Staff.objects.create(
            company=self.company, employee_number="S100", name="青木 太郎"
        )
        other = Staff.objects.create(
            company=self.company, employee_number="S200", name="田中 花子"
        )
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(
            company=self.company, symbol="○", meaning="対応可"
        )
        StaffSkill.objects.create(staff=target, work_type=work, level=level)
        StaffSkill.objects.create(staff=other, work_type=work, level=level)

        response = self.client.get(reverse("skill_map"), {"matrix_q": "青木"})

        self.assertContains(response, "青木 太郎")
        self.assertContains(response, f'name="skill_{target.pk}_{work.pk}"')
        self.assertNotContains(response, f'name="skill_{other.pk}_{work.pk}"')

    def test_skill_map_search_forms_are_independent(self):
        target = Staff.objects.create(
            company=self.company, employee_number="S100", name="青木 太郎"
        )
        other = Staff.objects.create(
            company=self.company, employee_number="S200", name="田中 花子"
        )
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(
            company=self.company, symbol="○", meaning="対応可"
        )
        StaffSkill.objects.create(staff=target, work_type=work, level=level)
        StaffSkill.objects.create(staff=other, work_type=work, level=level)

        matrix_response = self.client.get(reverse("skill_map"), {"matrix_q": "青木"})
        delete_response = self.client.get(reverse("skill_map"), {"delete_q": "田中"})

        self.assertContains(matrix_response, "田中 花子")
        self.assertContains(delete_response, f'name="skill_{target.pk}_{work.pk}"')
        self.assertContains(delete_response, f'name="skill_{other.pk}_{work.pk}"')
        self.assertContains(delete_response, "田中 花子")
        self.assertNotContains(delete_response, "青木 太郎</td>")

    def test_can_update_staff_skills_from_skill_map_matrix(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S100", name="青木 太郎"
        )
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(
            company=self.company, symbol="○", meaning="対応可"
        )

        response = self.client.post(
            reverse("skill_map"),
            {
                "action": "update_matrix",
                f"skill_{staff.pk}_{work.pk}": str(level.pk),
            },
        )

        self.assertRedirects(response, reverse("skill_map"))
        self.assertTrue(
            StaffSkill.objects.filter(
                staff=staff, work_type=work, level=level
            ).exists()
        )

    def test_can_clear_staff_skill_from_skill_map_matrix(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S100", name="青木 太郎"
        )
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(
            company=self.company, symbol="○", meaning="対応可"
        )
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)

        response = self.client.post(
            reverse("skill_map"),
            {
                "action": "update_matrix",
                f"skill_{staff.pk}_{work.pk}": "",
            },
        )

        self.assertRedirects(response, reverse("skill_map"))
        self.assertFalse(StaffSkill.objects.filter(staff=staff, work_type=work).exists())

    def test_bulk_delete_only_removes_own_company_skills(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S100", name="自社"
        )
        work = WorkType.objects.create(company=self.company, name="自社業務")
        level = SkillLevel.objects.create(
            company=self.company, symbol="◎", meaning="可"
        )
        own = StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        other_staff = Staff.objects.create(
            company=self.other_company, employee_number="O100", name="他社"
        )
        other_work = WorkType.objects.create(
            company=self.other_company, name="他社業務"
        )
        other_level = SkillLevel.objects.create(
            company=self.other_company, symbol="◎", meaning="可"
        )
        other = StaffSkill.objects.create(
            staff=other_staff, work_type=other_work, level=other_level
        )
        response = self.client.post(
            reverse("staff_skill_bulk_delete"),
            {"skill_ids": [own.pk, other.pk], "confirmed": "1"},
        )
        self.assertRedirects(response, reverse("skill_map"))
        self.assertFalse(StaffSkill.objects.filter(pk=own.pk).exists())
        self.assertTrue(StaffSkill.objects.filter(pk=other.pk).exists())

    def test_can_create_work_alternation_constraint(self):
        staff = Staff.objects.create(
            company=self.company, employee_number="S100", name="対象"
        )
        work_a = WorkType.objects.create(company=self.company, name="コンテナ")
        work_b = WorkType.objects.create(company=self.company, name="エーカス")
        rule_type = ConstraintType.objects.create(
            company=self.company,
            name="交互配置",
            operator=ConstraintType.Operator.WORK_ALTERNATION,
        )
        response = self.client.post(
            reverse("constraint_manage"),
            {
                "rule_type": rule_type.pk,
                "staff": staff.pk,
                "name": "コンテナとエーカスを交互にアサイン",
                "work_type_a": work_a.pk,
                "work_type_b": work_b.pk,
                "strength": "10",
                "active": "on",
            },
        )
        self.assertRedirects(response, reverse("constraint_manage"))
        constraint = IndividualConstraint.objects.get(
            name="コンテナとエーカスを交互にアサイン"
        )
        self.assertEqual(
            (constraint.work_type_a, constraint.work_type_b), (work_a, work_b)
        )
        self.assertEqual(constraint.strength, 10)
        self.assertTrue(constraint.is_hard)

    def test_can_search_constraints_by_staff_name_or_employee_number(self):
        staff_a = Staff.objects.create(
            company=self.company, employee_number="S100", name="青木"
        )
        staff_b = Staff.objects.create(
            company=self.company, employee_number="S200", name="田中"
        )
        IndividualConstraint.objects.create(
            company=self.company,
            staff=staff_a,
            name="青木の条件",
            kind="custom",
        )
        IndividualConstraint.objects.create(
            company=self.company,
            staff=staff_b,
            name="田中の条件",
            kind="custom",
        )

        name_response = self.client.get(reverse("constraint_manage"), {"q": "青木"})
        number_response = self.client.get(reverse("constraint_manage"), {"q": "S200"})

        self.assertContains(name_response, "青木の条件")
        self.assertNotContains(name_response, "田中の条件")
        self.assertContains(number_response, "田中の条件")
        self.assertNotContains(number_response, "青木の条件")
