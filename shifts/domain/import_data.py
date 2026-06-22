from dataclasses import dataclass


@dataclass(frozen=True)
class ImportedStaffRow:
    employee_number: str
    name: str
    note: str
    skills: dict[str, str]


@dataclass(frozen=True)
class ImportedSkillMap:
    rows: tuple[ImportedStaffRow, ...]
