from __future__ import annotations

import csv
from io import BytesIO, StringIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Side, Border

from shift_core.models import ShiftAssignment, ShiftPeriod, Staff, WorkType


def period_matrix(period: ShiftPeriod):
    staff_list = list(Staff.objects.filter(is_active=True).order_by("employee_number"))
    works = list(WorkType.objects.filter(active=True).order_by("display_order", "name"))
    assignments = {
        (item.staff_id, item.day): item
        for item in period.assignments.select_related("work_type")
    }
    days = sorted({item.day for item in period.assignments.all()})
    rows = []
    for staff in staff_list:
        cells = []
        for day in days:
            assignment = assignments.get((staff.id, day))
            cells.append({"day": day, "assignment": assignment, "label": _label(assignment)})
        rows.append({"staff": staff, "cells": cells})
    return days, rows, works


def build_shift_workbook(period: ShiftPeriod) -> bytes:
    days, rows, _works = period_matrix(period)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "シフト表"
    sheet.append([f"{period.month:%Y年%m月} シフト表"])
    sheet.append(["社員番号", "氏名", *[f"{day.day}日" for day in days], "公休", "有休", "出勤"])
    header_fill = PatternFill("solid", fgColor="E0F2FE")
    border = Border(bottom=Side(style="thin", color="CBD5E1"))
    for cell in sheet[2]:
        cell.fill = header_fill
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    for row_data in rows:
        public_holidays = sum(1 for cell in row_data["cells"] if cell["assignment"] and cell["assignment"].status == ShiftAssignment.Status.PUBLIC_HOLIDAY)
        paid_leaves = sum(1 for cell in row_data["cells"] if cell["assignment"] and cell["assignment"].status == ShiftAssignment.Status.PAID_LEAVE)
        work_days = sum(1 for cell in row_data["cells"] if cell["assignment"] and cell["assignment"].status in {ShiftAssignment.Status.WORK, ShiftAssignment.Status.STANDBY})
        sheet.append([
            row_data["staff"].employee_number,
            row_data["staff"].name,
            *[cell["label"] for cell in row_data["cells"]],
            public_holidays,
            paid_leaves,
            work_days,
        ])
    for row in sheet.iter_rows():
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "C3"
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def build_shift_csv(period: ShiftPeriod) -> str:
    days, rows, _works = period_matrix(period)
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow([f"{period.month:%Y年%m月} シフト表"])
    writer.writerow(["社員番号", "氏名", *[f"{day.day}日" for day in days]])
    for row_data in rows:
        writer.writerow([
            row_data["staff"].employee_number,
            row_data["staff"].name,
            *[cell["label"] for cell in row_data["cells"]],
        ])
    return stream.getvalue()


def _label(assignment):
    if not assignment:
        return ""
    if assignment.status == ShiftAssignment.Status.WORK:
        return assignment.work_type.name if assignment.work_type else "勤務"
    if assignment.status == ShiftAssignment.Status.PUBLIC_HOLIDAY:
        return "公休"
    if assignment.status == ShiftAssignment.Status.PAID_LEAVE:
        return "有休"
    if assignment.status == ShiftAssignment.Status.STANDBY:
        return "余剰"
    return ""
