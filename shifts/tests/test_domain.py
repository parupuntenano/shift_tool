from datetime import date
from unittest import TestCase

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
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
