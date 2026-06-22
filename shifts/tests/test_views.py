from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from shifts.infrastructure.models import Company, CompanyMembership, ConstraintType, IndividualConstraint, SkillLevel, Staff, StaffSkill, WorkType


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
        CompanyMembership.objects.create(company=self.company, user=self.user, role="staff")
        Staff.objects.create(company=self.company, user=self.user, employee_number="S001", name="青木")
        self.client.login(username="S001", password="0000")

    def test_staff_can_change_own_password(self):
        response = self.client.post(reverse("staff_change_password"), {
            "old_password": "0000",
            "new_password1": "new-secure-password-123",
            "new_password2": "new-secure-password-123",
        })
        self.assertRedirects(response, reverse("staff_change_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-password-123"))
        self.assertTrue(response.wsgi_request.user.is_authenticated)


class ManagerCrudTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="テスト", code="crud-test")
        self.other_company = Company.objects.create(name="他社", code="other")
        self.user = get_user_model().objects.create_user("crud-admin", password="pass")
        CompanyMembership.objects.create(company=self.company, user=self.user, role="admin")
        self.client.login(username="crud-admin", password="pass")

    def test_can_edit_work_in_own_company(self):
        work = WorkType.objects.create(company=self.company, name="旧業務", display_order=1)
        response = self.client.post(reverse("work_edit", args=[work.pk]), {
            "name": "新業務", "display_order": 2, "required_staff_per_day": 3, "active": "on"
        })
        self.assertRedirects(response, reverse("work_manage"))
        work.refresh_from_db()
        self.assertEqual((work.name, work.required_staff_per_day), ("新業務", 3))

    def test_cannot_edit_other_company_data(self):
        work = WorkType.objects.create(company=self.other_company, name="他社業務")
        response = self.client.get(reverse("work_edit", args=[work.pk]))
        self.assertEqual(response.status_code, 404)

    def test_can_delete_constraint_after_confirmation(self):
        rule = IndividualConstraint.objects.create(company=self.company, name="テスト条件", kind="custom")
        response = self.client.post(reverse("constraint_delete", args=[rule.pk]))
        self.assertRedirects(response, reverse("constraint_manage"))
        self.assertFalse(IndividualConstraint.objects.filter(pk=rule.pk).exists())

    def test_used_skill_level_is_protected_from_delete(self):
        staff = Staff.objects.create(company=self.company, employee_number="S1", name="A")
        work = WorkType.objects.create(company=self.company, name="受付")
        level = SkillLevel.objects.create(company=self.company, symbol="○", meaning="可")
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        response = self.client.post(reverse("skill_delete", args=[level.pk]))
        self.assertRedirects(response, reverse("skill_manage"))
        self.assertTrue(SkillLevel.objects.filter(pk=level.pk).exists())

    def test_skill_map_search_filters_by_staff_and_work(self):
        staff = Staff.objects.create(company=self.company, employee_number="S100", name="検索 太郎")
        work = WorkType.objects.create(company=self.company, name="検索対象業務")
        level = SkillLevel.objects.create(company=self.company, symbol="◎", meaning="リーダー")
        StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        response = self.client.get(reverse("skill_map"), {"q": "検索対象"})
        self.assertContains(response, "検索 太郎")
        self.assertContains(response, "検索対象業務")
        response = self.client.get(reverse("skill_map"), {"q": "存在しない"})
        self.assertContains(response, "条件に一致するスキル設定はありません。")
        self.assertNotContains(response, 'class="skill-checkbox"')

    def test_bulk_delete_only_removes_own_company_skills(self):
        staff = Staff.objects.create(company=self.company, employee_number="S100", name="自社")
        work = WorkType.objects.create(company=self.company, name="自社業務")
        level = SkillLevel.objects.create(company=self.company, symbol="◎", meaning="可")
        own = StaffSkill.objects.create(staff=staff, work_type=work, level=level)
        other_staff = Staff.objects.create(company=self.other_company, employee_number="O100", name="他社")
        other_work = WorkType.objects.create(company=self.other_company, name="他社業務")
        other_level = SkillLevel.objects.create(company=self.other_company, symbol="◎", meaning="可")
        other = StaffSkill.objects.create(staff=other_staff, work_type=other_work, level=other_level)
        response = self.client.post(reverse("staff_skill_bulk_delete"), {
            "skill_ids": [own.pk, other.pk], "confirmed": "1"
        })
        self.assertRedirects(response, reverse("skill_map"))
        self.assertFalse(StaffSkill.objects.filter(pk=own.pk).exists())
        self.assertTrue(StaffSkill.objects.filter(pk=other.pk).exists())

    def test_can_create_work_alternation_constraint(self):
        staff = Staff.objects.create(company=self.company, employee_number="S100", name="対象")
        work_a = WorkType.objects.create(company=self.company, name="コンテナ")
        work_b = WorkType.objects.create(company=self.company, name="エーカス")
        rule_type = ConstraintType.objects.create(
            company=self.company, name="交互配置", operator=ConstraintType.Operator.WORK_ALTERNATION
        )
        response = self.client.post(reverse("constraint_manage"), {
            "rule_type": rule_type.pk, "staff": staff.pk, "name": "コンテナとエーカスを交互にアサイン",
            "work_type_a": work_a.pk, "work_type_b": work_b.pk, "is_hard": "on", "active": "on",
        })
        self.assertRedirects(response, reverse("constraint_manage"))
        constraint = IndividualConstraint.objects.get(name="コンテナとエーカスを交互にアサイン")
        self.assertEqual((constraint.work_type_a, constraint.work_type_b), (work_a, work_b))
