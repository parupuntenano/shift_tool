from datetime import date
from io import BytesIO

from django.test import TestCase

from .models import ShiftAssignment, Staff, WorkType
from .services.generator import generate_monthly_shift
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
        self.assertEqual(Staff.objects.count(), 30)
        self.assertEqual(WorkType.objects.count(), 8)
        self.assertEqual(ShiftAssignment.objects.filter(period=period).count(), 930)
        self.assertFalse(
            period.warnings.filter(message__contains="必要人数を満たせません").exists()
        )

# Create your tests here.
