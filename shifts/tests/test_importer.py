from io import BytesIO
from unittest import TestCase

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from shifts.domain.import_data import ImportedSkillMap, ImportedStaffRow
from shifts.infrastructure.importers import SkillMapFileReader
from shifts.infrastructure.master_repository import DjangoMasterRepository
from shifts.infrastructure.models import Company, CompanyMembership, IndividualConstraint, Staff


class SkillMapFileReaderTests(TestCase):
    def test_reads_utf8_csv_to_domain_data(self):
        raw = "社員番号,氏名,備考,受付\nS001,青木,4勤不可,◎\n".encode("utf-8")
        result = SkillMapFileReader().read("skills.csv", BytesIO(raw))
        self.assertEqual(result.rows[0].employee_number, "S001")
        self.assertEqual(result.rows[0].skills, {"受付": "◎"})


class MasterImportTests(DjangoTestCase):
    def test_import_keeps_note_but_does_not_create_constraint(self):
        company = Company.objects.create(name="テスト", code="import-test")
        data = ImportedSkillMap((ImportedStaffRow("S001", "青木", "2勤1休", {"受付": "○"}),))
        DjangoMasterRepository().save_skill_map(company.id, data)
        self.assertEqual(Staff.objects.get(company=company).note, "2勤1休")
        self.assertFalse(IndividualConstraint.objects.filter(company=company).exists())

    def test_import_creates_staff_login_with_initial_password(self):
        company = Company.objects.create(name="テスト", code="account-import-test")
        data = ImportedSkillMap((ImportedStaffRow("50592", "青木", "", {}),))

        result = DjangoMasterRepository().save_skill_map(company.id, data)

        staff = Staff.objects.select_related("user").get(company=company, employee_number="50592")
        self.assertEqual(staff.user.username, "50592")
        self.assertTrue(staff.user.check_password("0000"))
        self.assertEqual(result["accounts"], 1)
        self.assertTrue(CompanyMembership.objects.filter(
            company=company, user=staff.user, role=CompanyMembership.Role.STAFF
        ).exists())

    def test_reimport_does_not_reset_existing_password(self):
        company = Company.objects.create(name="テスト", code="password-import-test")
        data = ImportedSkillMap((ImportedStaffRow("50592", "青木", "", {}),))
        repository = DjangoMasterRepository()
        repository.save_skill_map(company.id, data)
        user = get_user_model().objects.get(username="50592")
        user.set_password("changed-password")
        user.save()

        result = repository.save_skill_map(company.id, data)

        user.refresh_from_db()
        self.assertTrue(user.check_password("changed-password"))
        self.assertEqual(result["accounts"], 0)
