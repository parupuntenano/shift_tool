from datetime import date
from typing import BinaryIO, Protocol

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
    GenerationResult,
    PreviousShiftDay,
    SkillRating,
    StaffMember,
    Work,
)
from shifts.domain.import_data import ImportedSkillMap


class ShiftRepository(Protocol):
    def staff_for_generation(self, company_id: int) -> list[StaffMember]: ...
    def works_for_generation(self, company_id: int) -> list[Work]: ...
    def skills_for_generation(self, company_id: int) -> list[SkillRating]: ...
    def availability_for_generation(
        self, company_id: int, month: date
    ) -> list[Availability]: ...
    def previous_shift_days_for_generation(
        self, company_id: int, month: date
    ) -> list[PreviousShiftDay]: ...
    def rules_for_generation(self, company_id: int) -> list[ConstraintRule]: ...
    def save_generation(
        self, company_id: int, month: date, result: GenerationResult
    ) -> int: ...


class SkillMapReader(Protocol):
    def read(self, filename: str, file_obj: BinaryIO) -> ImportedSkillMap: ...


class MasterRepository(Protocol):
    def save_skill_map(
        self, company_id: int, data: ImportedSkillMap
    ) -> dict[str, int]: ...
