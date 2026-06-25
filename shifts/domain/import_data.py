from dataclasses import dataclass


@dataclass(frozen=True)
class ImportedStaffRow:
    employee_number: str
    name: str
    note: str
    skills: dict[str, str]
    monthly_public_holidays: int = 8
    desired_off_limit: int = 4


@dataclass(frozen=True)
class ImportedSkillLevel:
    symbol: str
    meaning: str
    priority: int
    assignable: bool


@dataclass(frozen=True)
class ImportedWorkType:
    name: str
    minimum_staff_per_day: int
    active: bool


@dataclass(frozen=True)
class ImportedSkillMap:
    rows: tuple[ImportedStaffRow, ...]
    skill_levels: tuple[ImportedSkillLevel, ...] = ()
    work_types: tuple[ImportedWorkType, ...] = ()
