from datetime import date

from django import forms
from shifts.infrastructure.models import (
    ConstraintType,
    IndividualConstraint,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)


class MonthInput(forms.DateInput):
    input_type = "month"


class MonthField(forms.DateField):
    widget = MonthInput

    def to_python(self, value):
        if isinstance(value, str) and len(value) == 7:
            try:
                year, month = map(int, value.split("-"))
                return date(year, month, 1)
            except (TypeError, ValueError):
                pass
        return super().to_python(value)


class StaffForm(forms.ModelForm):
    class Meta:
        model = Staff
        fields = [
            "employee_number",
            "name",
            "monthly_public_holidays",
            "is_employee",
            "note",
            "active",
        ]
        widgets = {
            "monthly_public_holidays": forms.NumberInput(attrs={"min": 0}),
        }

    def save_for_company(self, company, commit=True):
        staff = super().save(commit=False)
        is_new = staff.pk is None
        staff.company = company
        if is_new:
            staff.desired_off_limit = company.default_desired_off_limit
        if commit:
            staff.save()
        return staff


class BulkDesiredOffLimitForm(forms.Form):
    desired_off_limit = forms.IntegerField(
        label="公有給希望上限",
        min_value=0,
        help_text="公休希望と有給希望を合わせた申請上限を、全スタッフへ反映します。",
    )


class PreviousShiftImportForm(forms.Form):
    month = MonthField(
        label="実績月",
        help_text="取り込む先月シフト実績の月を選択してください。",
    )
    file = forms.FileField(label="先月シフト実績ファイル")


class AvailabilityImportForm(forms.Form):
    month = MonthField(label="対象月")
    file = forms.FileField(
        label="公休申請ファイル",
        help_text="社員番号列がある .xlsx / .csv を取り込めます。",
    )


class WorkTypeForm(forms.ModelForm):
    class Meta:
        model = WorkType
        fields = ["name", "display_order", "required_staff_per_day", "color", "active"]
        widgets = {
            "color": forms.TextInput(attrs={"type": "color"}),
        }

    def save_for_company(self, company):
        item = self.save(commit=False)
        item.company = company
        item.save()
        return item


class SkillLevelForm(forms.ModelForm):
    class Meta:
        model = SkillLevel
        fields = ["symbol", "meaning", "priority", "assignable"]

    def save_for_company(self, company):
        item = self.save(commit=False)
        item.company = company
        item.display_order = item.priority
        item.save()
        return item


class ConstraintTypeForm(forms.ModelForm):
    class Meta:
        model = ConstraintType
        fields = ["name", "operator", "description", "default_strength", "active"]
        labels = {
            "name": "ルール種別名",
        }
        widgets = {
            "default_strength": forms.NumberInput(attrs={"min": 1, "max": 10}),
        }

    def save_for_company(self, company):
        item = self.save(commit=False)
        item.company = company
        item.default_is_hard = item.default_strength == 10
        item.save()
        return item


class ConstraintForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="対象曜日",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[
            ("0", "月"),
            ("1", "火"),
            ("2", "水"),
            ("3", "木"),
            ("4", "金"),
            ("5", "土"),
            ("6", "日"),
        ],
    )

    class Meta:
        model = IndividualConstraint
        fields = [
            "rule_type",
            "staff",
            "name",
            "related_staff",
            "work_type_a",
            "work_type_b",
            "numeric_value",
            "text_value",
            "weekdays",
            "strength",
            "active",
        ]
        labels = {
            "rule_type": "ルール種別",
            "name": "ルール名",
        }
        widgets = {
            "strength": forms.NumberInput(attrs={"min": 1, "max": 10}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = (
            Staff.objects.filter(company=company) if company else Staff.objects.none()
        )
        self.fields["related_staff"].queryset = (
            Staff.objects.filter(company=company) if company else Staff.objects.none()
        )
        works = (
            WorkType.objects.filter(company=company, active=True)
            if company
            else WorkType.objects.none()
        )
        self.fields["work_type_a"].queryset = works
        self.fields["work_type_b"].queryset = works
        self.fields["rule_type"].queryset = (
            ConstraintType.objects.filter(company=company, active=True)
            if company
            else ConstraintType.objects.none()
        )
        self.fields["strength"].help_text = (
            "10は絶対守る、1〜9は数字が大きいほど強く優先します。"
        )
        if not self.instance.pk:
            self.fields["strength"].initial = 10
        self.fields["numeric_value"].help_text = (
            "最大連続勤務日数など、選択した判定方式で必要な場合に入力"
        )
        if self.instance and self.instance.pk:
            self.fields["weekdays"].initial = [
                str(value) for value in self.instance.weekdays
            ]

    def clean(self):
        cleaned = super().clean()
        rule_type = cleaned.get("rule_type")
        if not rule_type:
            return cleaned
        strength = cleaned.get("strength")
        if strength is not None and not 1 <= strength <= 10:
            self.add_error("strength", "強度は1〜10で入力してください。")
        operator = rule_type.operator
        staff_required_operators = {
            ConstraintType.Operator.MAX_CONSECUTIVE,
            ConstraintType.Operator.WORK_ALTERNATION,
            ConstraintType.Operator.INCOMPATIBLE_SAME_WORK,
            ConstraintType.Operator.WORK_REST_PATTERN,
            ConstraintType.Operator.NO_SINGLE_REST,
            ConstraintType.Operator.AVOID_SPECIFIC_WORK,
            ConstraintType.Operator.FORBID_SPECIFIC_WORK,
        }
        if operator in staff_required_operators and not cleaned.get("staff"):
            self.add_error("staff", "この判定方式では対象スタッフが必要です。")
        if operator == ConstraintType.Operator.MAX_CONSECUTIVE and not cleaned.get(
            "numeric_value"
        ):
            self.add_error("numeric_value", "最大連続勤務日数を入力してください。")
        if operator == ConstraintType.Operator.WORK_ALTERNATION:
            if not cleaned.get("work_type_a") or not cleaned.get("work_type_b"):
                self.add_error(
                    "work_type_a", "交互に配置する2つの業務を選択してください。"
                )
            elif cleaned.get("work_type_a") == cleaned.get("work_type_b"):
                self.add_error("work_type_b", "異なる業務を選択してください。")
        if operator == ConstraintType.Operator.INCOMPATIBLE_SAME_WORK:
            if not cleaned.get("related_staff"):
                self.add_error(
                    "related_staff",
                    "同時配置を禁止する相手スタッフを選択してください。",
                )
            elif cleaned.get("staff") == cleaned.get("related_staff"):
                self.add_error(
                    "related_staff", "対象スタッフとは別のスタッフを選択してください。"
                )
        if operator == ConstraintType.Operator.WORK_REST_PATTERN:
            pattern = cleaned.get("text_value", "").replace("、", ",")
            try:
                counts = [
                    int(part.strip()) for part in pattern.split(",") if part.strip()
                ]
            except ValueError:
                counts = []
            if not counts or len(counts) % 2 or any(value < 1 for value in counts):
                self.add_error(
                    "text_value",
                    "勤務日数,休日日数の順で入力してください。例: 2,1 または 2,1,3,1",
                )
        if operator in {
            ConstraintType.Operator.AVOID_SPECIFIC_WORK,
            ConstraintType.Operator.FORBID_SPECIFIC_WORK,
        } and not cleaned.get("work_type_a"):
            self.add_error("work_type_a", "対象業務を選択してください。")
        if (
            operator == ConstraintType.Operator.FORBID_WORKS_ON_WEEKDAYS
            and not cleaned.get("weekdays")
        ):
            self.add_error("weekdays", "対象曜日を1つ以上選択してください。")
        return cleaned

    def save_for_company(self, company):
        item = self.save(commit=False)
        item.company = company
        item.kind = (
            item.rule_type.operator
            if item.rule_type
            else IndividualConstraint.Kind.CUSTOM
        )
        item.parameters = {"value": item.numeric_value} if item.numeric_value else {}
        item.weekdays = [int(value) for value in self.cleaned_data.get("weekdays", [])]
        item.is_hard = item.strength == 10
        item.save()
        return item


class StaffSkillForm(forms.ModelForm):
    class Meta:
        model = StaffSkill
        fields = ["staff", "work_type", "level"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = (
            Staff.objects.filter(company=company, active=True)
            if company
            else Staff.objects.none()
        )
        self.fields["work_type"].queryset = (
            WorkType.objects.filter(company=company, active=True)
            if company
            else WorkType.objects.none()
        )
        self.fields["level"].queryset = (
            SkillLevel.objects.filter(company=company)
            if company
            else SkillLevel.objects.none()
        )


class GenerateForm(forms.Form):
    month = forms.DateField(
        label="対象月", widget=MonthInput(format="%Y-%m"), input_formats=["%Y-%m"]
    )


class ImportForm(forms.Form):
    file = forms.FileField(label="スキル表ファイル", help_text=".xlsx / .xls / .csv")

    def clean_file(self):
        file = self.cleaned_data["file"]
        if not file.name.lower().endswith((".xlsx", ".xls", ".csv")):
            raise forms.ValidationError(
                ".xlsx、.xls、.csvのいずれかを選択してください。"
            )
        return file
