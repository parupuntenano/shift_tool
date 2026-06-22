from django.contrib import admin
from shifts.infrastructure import models

for model in (
    models.Company,
    models.CompanyMembership,
    models.Staff,
    models.WorkType,
    models.SkillLevel,
    models.ConstraintType,
    models.StaffSkill,
    models.IndividualConstraint,
    models.AvailabilitySubmission,
    models.AvailabilityDay,
    models.ShiftPeriod,
    models.ShiftAssignment,
    models.GenerationWarning,
    models.ImportJob,
):
    admin.site.register(model)
