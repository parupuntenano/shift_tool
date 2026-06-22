from dataclasses import dataclass
from datetime import date

from shifts.domain.generator import MonthlyShiftGenerator
from .ports import MasterRepository, ShiftRepository, SkillMapReader


@dataclass(frozen=True)
class GenerateMonthlyShiftOutput:
    period_id: int
    assignment_count: int
    warning_count: int


class GenerateMonthlyShift:
    def __init__(self, repository: ShiftRepository, generator: MonthlyShiftGenerator | None = None):
        self.repository = repository
        self.generator = generator or MonthlyShiftGenerator()

    def execute(self, company_id: int, month: date) -> GenerateMonthlyShiftOutput:
        month = month.replace(day=1)
        result = self.generator.generate(
            month,
            self.repository.staff_for_generation(company_id),
            self.repository.works_for_generation(company_id),
            self.repository.skills_for_generation(company_id),
            self.repository.availability_for_generation(company_id, month),
            self.repository.rules_for_generation(company_id),
        )
        period_id = self.repository.save_generation(company_id, month, result)
        return GenerateMonthlyShiftOutput(period_id, len(result.assignments), len(result.warnings))


class ImportSkillMap:
    def __init__(self, reader: SkillMapReader, repository: MasterRepository):
        self.reader = reader
        self.repository = repository

    def execute(self, company_id: int, filename: str, file_obj) -> dict[str, int]:
        parsed = self.reader.read(filename, file_obj)
        if not parsed.rows:
            raise ValueError("取込対象のデータがありません。")
        return self.repository.save_skill_map(company_id, parsed)
