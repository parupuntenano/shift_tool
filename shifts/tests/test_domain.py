from datetime import date
from unittest import TestCase

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
    PreviousShiftDay,
    SkillRating,
    StaffMember,
    Work,
)
from shifts.domain.generator import MonthlyShiftGenerator


class MonthlyShiftGeneratorTests(TestCase):
    def test_uses_only_available_assignable_staff_and_reports_shortage(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A"), StaffMember(2, "B")]
        works = [Work(10, "業務", 2)]
        skills = [SkillRating(1, 10, 1, True), SkillRating(2, 10, 1, False)]
        availability = [Availability(1, month, True), Availability(2, month, True)]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability
        )
        self.assertEqual(len(result.assignments), 1)
        self.assertEqual(result.assignments[0].staff_id, 1)
        self.assertEqual(len(result.warnings), 28)

    def test_respects_preferred_off_and_max_consecutive_days(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A", max_consecutive_days=2)]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=d), True, preferred_off=d == 2)
            for d in range(1, 6)
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability
        )
        days = [item.day.day for item in result.assignments]
        self.assertNotIn(2, days)
        self.assertNotIn(5, days)

    def test_alternates_between_two_configured_works(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "コンテナ", 1, 1), Work(20, "エーカス", 1, 2)]
        skills = [SkillRating(1, 10, 1, True), SkillRating(1, 20, 1, True)]
        availability = [
            Availability(1, month.replace(day=d), True) for d in range(1, 5)
        ]
        rules = [
            ConstraintRule(
                "work_alternation", staff_id=1, work_ids=(10, 20), is_hard=True
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertEqual(
            [(item.day.day, item.work_id) for item in result.assignments],
            [(1, 10), (2, 20), (3, 10), (4, 20)],
        )

    def test_alternation_uses_previous_month_last_work(self):
        month = date(2026, 7, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "ロール", 1, 1), Work(20, "エーカス", 1, 2)]
        skills = [SkillRating(1, 10, 1, True), SkillRating(1, 20, 1, True)]
        availability = [Availability(1, month, True)]
        rules = [
            ConstraintRule(
                "work_alternation", staff_id=1, work_ids=(10, 20), is_hard=True
            )
        ]
        previous = [PreviousShiftDay(1, date(2026, 6, 30), "work", 10)]

        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules, previous
        )

        self.assertEqual(
            [(item.day.day, item.work_id) for item in result.assignments],
            [(1, 20)],
        )

    def test_places_at_least_one_staff_on_each_work_before_extra_slots(self):
        month = date(2026, 2, 1)
        staff = [
            StaffMember(1, "A"),
            StaffMember(2, "B"),
            StaffMember(3, "C"),
        ]
        works = [Work(10, "業務A", 2, 1), Work(20, "業務B", 1, 2)]
        skills = [
            SkillRating(1, 10, 1, True),
            SkillRating(2, 10, 1, True),
            SkillRating(2, 20, 1, True),
            SkillRating(3, 20, 1, True),
        ]
        availability = [Availability(staff_id, month, True) for staff_id in (1, 2, 3)]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability
        )
        day_one_work_ids = {
            item.work_id for item in result.assignments if item.day == month
        }
        self.assertEqual(day_one_work_ids, {10, 20})

    def test_trainee_is_assigned_only_with_instructor_on_same_work(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "指導者"), StaffMember(2, "研修中")]
        works = [Work(10, "受付", 2)]
        skills = [
            SkillRating(
                1,
                10,
                1,
                True,
                instructor_capable=True,
            ),
            SkillRating(
                2,
                10,
                3,
                True,
                trainee=True,
            ),
        ]
        availability = [Availability(1, month, True), Availability(2, month, True)]

        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability
        )

        self.assertEqual(
            sorted((item.staff_id, item.work_id) for item in result.assignments),
            [(1, 10), (2, 10)],
        )

    def test_trainee_is_not_assigned_without_instructor(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "研修中")]
        works = [Work(10, "受付", 1)]
        skills = [SkillRating(1, 10, 3, True, trainee=True)]
        availability = [Availability(1, month, True)]

        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability
        )

        self.assertFalse(result.assignments)
        self.assertTrue(result.warnings)

    def test_incompatible_staff_are_not_assigned_to_same_work(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A"), StaffMember(2, "B")]
        works = [Work(10, "業務", 2)]
        skills = [SkillRating(1, 10, 1, True), SkillRating(2, 10, 1, True)]
        availability = [Availability(staff_id, month, True) for staff_id in (1, 2)]
        rules = [
            ConstraintRule(
                "incompatible_same_work", staff_id=1, related_staff_id=2, is_hard=True
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        day_one = [item for item in result.assignments if item.day == month]
        self.assertEqual(len(day_one), 1)
        self.assertTrue(
            any(item.day == month and item.work_id == 10 for item in result.warnings)
        )

    def test_strength_10_blocks_assignment_even_when_is_hard_is_false(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [Availability(1, month, True)]
        rules = [
            ConstraintRule(
                "forbid_specific_work",
                staff_id=1,
                work_ids=(10,),
                is_hard=False,
                strength=10,
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertFalse(any(item.day == month for item in result.assignments))

    def test_strength_below_10_is_soft_penalty(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [Availability(1, month, True)]
        rules = [
            ConstraintRule(
                "forbid_specific_work",
                staff_id=1,
                work_ids=(10,),
                is_hard=True,
                strength=5,
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertTrue(any(item.day == month for item in result.assignments))

    def test_max_consecutive_uses_strength_as_soft_or_hard(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A", max_consecutive_days=10)]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=1), True),
            Availability(1, month.replace(day=2), True),
        ]
        soft_result = MonthlyShiftGenerator().generate(
            month,
            staff,
            works,
            skills,
            availability,
            [ConstraintRule("max_consecutive", staff_id=1, numeric_value=1, strength=5)],
        )
        hard_result = MonthlyShiftGenerator().generate(
            month,
            staff,
            works,
            skills,
            availability,
            [ConstraintRule("max_consecutive", staff_id=1, numeric_value=1, strength=10)],
        )
        self.assertEqual(
            [item.day.day for item in soft_result.assignments if item.day.day <= 2],
            [1, 2],
        )
        self.assertEqual(
            [item.day.day for item in hard_result.assignments if item.day.day <= 2],
            [1],
        )

    def test_applies_work_rest_pattern(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=d), True) for d in range(1, 7)
        ]
        rules = [
            ConstraintRule(
                "work_rest_pattern", staff_id=1, text_value="2,1", is_hard=True
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertEqual([item.day.day for item in result.assignments], [1, 2, 4, 5])

    def test_work_rest_pattern_uses_previous_month_last_public_holiday(self):
        month = date(2026, 7, 1)
        staff = [StaffMember(1, "A", max_consecutive_days=10)]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=d), True) for d in range(1, 4)
        ]
        rules = [
            ConstraintRule(
                "work_rest_pattern", staff_id=1, text_value="2,1", strength=10
            )
        ]
        previous = [PreviousShiftDay(1, date(2026, 6, 30), "public_holiday")]

        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules, previous
        )

        self.assertEqual([item.day.day for item in result.assignments], [1, 2])

    def test_prevents_single_rest_between_work_days(self):
        month = date(2026, 2, 1)
        staff = [StaffMember(1, "A")]
        works = [Work(10, "業務", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=1), True),
            Availability(1, month.replace(day=2), False),
            Availability(1, month.replace(day=3), True),
            Availability(1, month.replace(day=4), True),
        ]
        rules = [ConstraintRule("no_single_rest", staff_id=1, is_hard=True)]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertEqual([item.day.day for item in result.assignments], [1, 4])

    def test_blocks_specific_work_on_selected_weekday(self):
        month = date(2026, 2, 1)  # Sunday
        staff = [StaffMember(1, "A")]
        works = [Work(10, "NZ", 1)]
        skills = [SkillRating(1, 10, 1, True)]
        availability = [
            Availability(1, month.replace(day=d), True) for d in range(1, 7)
        ]
        rules = [
            ConstraintRule(
                "forbid_works_on_weekdays",
                staff_id=1,
                work_ids=(10,),
                weekdays=(4,),
                is_hard=True,
            )
        ]
        result = MonthlyShiftGenerator().generate(
            month, staff, works, skills, availability, rules
        )
        self.assertNotIn(6, [item.day.day for item in result.assignments])
