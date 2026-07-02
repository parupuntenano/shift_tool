from django.contrib import admin

from .models import (
    PreviousShiftRecord,
    ShiftAssignment,
    ShiftPeriod,
    ShiftWarning,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)


admin.site.register(Staff)
admin.site.register(WorkType)
admin.site.register(SkillLevel)
admin.site.register(StaffSkill)
admin.site.register(PreviousShiftRecord)
admin.site.register(ShiftPeriod)
admin.site.register(ShiftAssignment)
admin.site.register(ShiftWarning)

# Register your models here.
