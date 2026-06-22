from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
    GenerationResult,
    SkillRating,
    StaffMember,
    Work,
)
from .models import (
    AvailabilityDay,
    AvailabilitySubmission,
    GenerationWarning,
    IndividualConstraint,
    ShiftAssignment,
    ShiftPeriod,
    Staff,
    StaffSkill,
    WorkType,
)


class DjangoShiftRepository:
    def staff_for_generation(self, company_id: int) -> list[StaffMember]:
        limits = {}
        rules = IndividualConstraint.objects.filter(
            Q(kind=IndividualConstraint.Kind.MAX_CONSECUTIVE)
            | Q(rule_type__operator="max_consecutive"),
            company_id=company_id,
            active=True,
        ).exclude(staff=None)
        for rule in rules:
            limits[rule.staff_id] = int(
                rule.numeric_value or rule.parameters.get("days", 6)
            )
        return [
            StaffMember(row.id, row.name, limits.get(row.id, 6))
            for row in Staff.objects.filter(company_id=company_id, active=True)
        ]

    def works_for_generation(self, company_id: int) -> list[Work]:
        return [
            Work(row.id, row.name, row.required_staff_per_day, row.display_order)
            for row in WorkType.objects.filter(company_id=company_id, active=True)
        ]

    def skills_for_generation(self, company_id: int) -> list[SkillRating]:
        rows = StaffSkill.objects.filter(staff__company_id=company_id).select_related(
            "level"
        )
        return [
            SkillRating(
                row.staff_id, row.work_type_id, row.level.priority, row.level.assignable
            )
            for row in rows
        ]

    def availability_for_generation(
        self, company_id: int, month: date
    ) -> list[Availability]:
        rows = AvailabilityDay.objects.filter(
            submission__staff__company_id=company_id,
            submission__month=month.replace(day=1),
            submission__status=AvailabilitySubmission.Status.SUBMITTED,
        )
        return [
            Availability(
                row.submission.staff_id, row.day, row.available, row.preferred_off
            )
            for row in rows.select_related("submission")
        ]

    def rules_for_generation(self, company_id: int) -> list[ConstraintRule]:
        rows = IndividualConstraint.objects.filter(
            company_id=company_id, active=True, rule_type__isnull=False
        ).select_related("rule_type")
        result = []
        for row in rows:
            operator = row.rule_type.operator
            if operator in {"custom", "max_consecutive"}:
                continue
            result.append(
                ConstraintRule(
                    operator=operator,
                    staff_id=row.staff_id,
                    related_staff_id=row.related_staff_id,
                    work_ids=tuple(
                        value
                        for value in (row.work_type_a_id, row.work_type_b_id)
                        if value
                    ),
                    numeric_value=row.numeric_value,
                    text_value=row.text_value,
                    weekdays=tuple(int(value) for value in row.weekdays),
                    is_hard=row.is_hard,
                )
            )
        return result

    @transaction.atomic
    def save_generation(
        self, company_id: int, month: date, result: GenerationResult
    ) -> int:
        period, _ = ShiftPeriod.objects.get_or_create(
            company_id=company_id, month=month.replace(day=1)
        )
        period.assignments.all().delete()
        period.warnings.all().delete()
        ShiftAssignment.objects.bulk_create(
            [
                ShiftAssignment(
                    period=period,
                    staff_id=item.staff_id,
                    work_type_id=item.work_id,
                    day=item.day,
                )
                for item in result.assignments
            ]
        )
        GenerationWarning.objects.bulk_create(
            [
                GenerationWarning(
                    period=period,
                    day=item.day,
                    work_type_id=item.work_id,
                    message=item.message,
                )
                for item in result.warnings
            ]
        )
        period.status = ShiftPeriod.Status.DRAFT
        period.warning_count = len(result.warnings)
        period.generated_at = timezone.now()
        period.save(update_fields=["status", "warning_count", "generated_at"])
        return period.id
