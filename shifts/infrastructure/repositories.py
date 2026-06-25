import calendar
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
    GenerationResult,
    PreviousShiftDay,
    SkillRating,
    StaffMember,
    Work,
)
from .models import (
    AvailabilityDay,
    AvailabilitySubmission,
    GenerationWarning,
    IndividualConstraint,
    PreviousMonthShiftDay,
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
            if rule.strength < 10:
                continue
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
                row.staff_id,
                row.work_type_id,
                row.level.priority,
                row.level.assignable,
                self._is_instructor_level(row.level),
                self._is_trainee_level(row.level),
            )
            for row in rows
        ]

    @staticmethod
    def _is_instructor_level(level) -> bool:
        text = f"{level.symbol} {level.meaning}".replace(" ", "").replace("　", "")
        return any(word in text for word in ("指導", "教官", "主担当", "リーダー"))

    @staticmethod
    def _is_trainee_level(level) -> bool:
        text = f"{level.symbol} {level.meaning}".replace(" ", "").replace("　", "")
        return any(word in text for word in ("研修", "訓練"))

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
                row.submission.staff_id,
                row.day,
                row.available,
                row.preferred_off,
                row.paid_leave,
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
            if operator == "custom":
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
                    strength=row.strength,
                )
            )
        return result

    def previous_shift_days_for_generation(
        self, company_id: int, month: date
    ) -> list[PreviousShiftDay]:
        previous_month = self._add_months(month.replace(day=1), -1)
        period = (
            ShiftPeriod.objects.filter(
                company_id=company_id,
                month=previous_month,
                assignments__isnull=False,
            )
            .distinct()
            .first()
        )
        if period:
            return self._previous_shift_days_from_period(company_id, period)

        rows = PreviousMonthShiftDay.objects.filter(
            company_id=company_id,
            day__year=previous_month.year,
            day__month=previous_month.month,
        )
        return [
            PreviousShiftDay(
                row.staff_id,
                row.day,
                row.status,
                row.work_type_id,
            )
            for row in rows
        ]

    def _previous_shift_days_from_period(
        self, company_id: int, period: ShiftPeriod
    ) -> list[PreviousShiftDay]:
        last_day_number = calendar.monthrange(period.month.year, period.month.month)[1]
        first_day = period.month.replace(day=last_day_number - 6)
        last_day = period.month.replace(day=last_day_number)
        staff_ids = list(
            Staff.objects.filter(company_id=company_id, active=True).values_list(
                "id", flat=True
            )
        )
        assignment_map = {
            (item.staff_id, item.day): item.work_type_id
            for item in ShiftAssignment.objects.filter(
                period=period,
                day__range=(first_day, last_day),
            )
        }
        paid_leave_days = {
            (item.submission.staff_id, item.day)
            for item in AvailabilityDay.objects.filter(
                submission__staff__company_id=company_id,
                submission__month=period.month,
                submission__status=AvailabilitySubmission.Status.SUBMITTED,
                paid_leave=True,
                day__range=(first_day, last_day),
            ).select_related("submission")
        }
        result = []
        for staff_id in staff_ids:
            for day_number in range(first_day.day, last_day.day + 1):
                day = period.month.replace(day=day_number)
                work_id = assignment_map.get((staff_id, day))
                if work_id:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.WORK,
                            work_id,
                        )
                    )
                elif (staff_id, day) in paid_leave_days:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.PAID_LEAVE,
                        )
                    )
                else:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY,
                        )
                    )
        return result

    @staticmethod
    def _add_months(month: date, offset: int) -> date:
        year = month.year + (month.month - 1 + offset) // 12
        month_number = (month.month - 1 + offset) % 12 + 1
        return month.replace(year=year, month=month_number, day=1)

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
