from django import forms
from django.contrib.auth import get_user_model

from shifts.infrastructure.models import ConstraintType, IndividualConstraint, SkillLevel, Staff, StaffSkill, WorkType


class MonthInput(forms.DateInput): input_type = "month"


class StaffForm(forms.ModelForm):
    username = forms.CharField(label="ログインID", required=False, help_text="未入力の場合は社員番号を使用します")
    password = forms.CharField(
        label="パスワード", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        required=False, help_text="編集時に空欄のままなら現在のパスワードを維持します",
    )
    class Meta:
        model = Staff; fields = ["employee_number", "name", "username", "password", "note", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.user:
            self.fields["username"].initial = self.instance.user.username

    def save_for_company(self, company, commit=True):
        staff = super().save(commit=False); staff.company = company
        username = self.cleaned_data.get("username") or staff.employee_number
        password = self.cleaned_data.get("password")
        if username:
            user, created = get_user_model().objects.get_or_create(username=username)
            if password:
                user.set_password(password)
            elif created:
                user.set_password("0000")
            if created or password:
                user.save()
            staff.user = user
        if commit: staff.save()
        return staff


class WorkTypeForm(forms.ModelForm):
    class Meta: model = WorkType; fields = ["name", "display_order", "required_staff_per_day", "active"]
    def save_for_company(self, company):
        item = self.save(commit=False); item.company = company; item.save(); return item


class SkillLevelForm(forms.ModelForm):
    class Meta: model = SkillLevel; fields = ["symbol", "meaning", "priority", "assignable", "display_order"]
    def save_for_company(self, company):
        item = self.save(commit=False); item.company = company; item.save(); return item


class ConstraintTypeForm(forms.ModelForm):
    class Meta:
        model = ConstraintType
        fields = ["name", "operator", "description", "default_is_hard", "active"]
    def save_for_company(self, company):
        item = self.save(commit=False); item.company = company; item.save(); return item


class ConstraintForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="対象曜日", required=False, widget=forms.CheckboxSelectMultiple,
        choices=[("0", "月"), ("1", "火"), ("2", "水"), ("3", "木"), ("4", "金"), ("5", "土"), ("6", "日")],
    )
    class Meta:
        model = IndividualConstraint
        fields = ["rule_type", "staff", "name", "related_staff", "work_type_a", "work_type_b", "numeric_value", "text_value", "weekdays", "is_hard", "active"]
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = Staff.objects.filter(company=company) if company else Staff.objects.none()
        self.fields["related_staff"].queryset = Staff.objects.filter(company=company) if company else Staff.objects.none()
        works = WorkType.objects.filter(company=company, active=True) if company else WorkType.objects.none()
        self.fields["work_type_a"].queryset = works
        self.fields["work_type_b"].queryset = works
        self.fields["rule_type"].queryset = ConstraintType.objects.filter(company=company, active=True) if company else ConstraintType.objects.none()
        self.fields["numeric_value"].help_text = "最大連続勤務日数など、選択した判定方式で必要な場合に入力"
        if self.instance and self.instance.pk:
            self.fields["weekdays"].initial = [str(value) for value in self.instance.weekdays]

    def clean(self):
        cleaned = super().clean()
        rule_type = cleaned.get("rule_type")
        if not rule_type:
            return cleaned
        operator = rule_type.operator
        staff_required_operators = {
            ConstraintType.Operator.MAX_CONSECUTIVE, ConstraintType.Operator.WORK_ALTERNATION,
            ConstraintType.Operator.INCOMPATIBLE_SAME_WORK, ConstraintType.Operator.WORK_REST_PATTERN,
            ConstraintType.Operator.NO_SINGLE_REST, ConstraintType.Operator.AVOID_SPECIFIC_WORK,
            ConstraintType.Operator.FORBID_SPECIFIC_WORK,
        }
        if operator in staff_required_operators and not cleaned.get("staff"):
            self.add_error("staff", "この判定方式では対象スタッフが必要です。")
        if operator == ConstraintType.Operator.MAX_CONSECUTIVE and not cleaned.get("numeric_value"):
            self.add_error("numeric_value", "最大連続勤務日数を入力してください。")
        if operator == ConstraintType.Operator.WORK_ALTERNATION:
            if not cleaned.get("work_type_a") or not cleaned.get("work_type_b"):
                self.add_error("work_type_a", "交互に配置する2つの業務を選択してください。")
            elif cleaned.get("work_type_a") == cleaned.get("work_type_b"):
                self.add_error("work_type_b", "異なる業務を選択してください。")
        if operator == ConstraintType.Operator.INCOMPATIBLE_SAME_WORK:
            if not cleaned.get("related_staff"):
                self.add_error("related_staff", "同時配置を禁止する相手スタッフを選択してください。")
            elif cleaned.get("staff") == cleaned.get("related_staff"):
                self.add_error("related_staff", "対象スタッフとは別のスタッフを選択してください。")
        if operator == ConstraintType.Operator.WORK_REST_PATTERN:
            pattern = cleaned.get("text_value", "").replace("、", ",")
            try:
                counts = [int(part.strip()) for part in pattern.split(",") if part.strip()]
            except ValueError:
                counts = []
            if not counts or len(counts) % 2 or any(value < 1 for value in counts):
                self.add_error("text_value", "勤務日数,休日日数の順で入力してください。例: 2,1 または 2,1,3,1")
        if operator in {ConstraintType.Operator.AVOID_SPECIFIC_WORK, ConstraintType.Operator.FORBID_SPECIFIC_WORK} and not cleaned.get("work_type_a"):
            self.add_error("work_type_a", "対象業務を選択してください。")
        if operator == ConstraintType.Operator.FORBID_WORKS_ON_WEEKDAYS and not cleaned.get("weekdays"):
            self.add_error("weekdays", "対象曜日を1つ以上選択してください。")
        return cleaned

    def save_for_company(self, company):
        item = self.save(commit=False); item.company = company
        item.kind = item.rule_type.operator if item.rule_type else IndividualConstraint.Kind.CUSTOM
        item.parameters = {"value": item.numeric_value} if item.numeric_value else {}
        item.weekdays = [int(value) for value in self.cleaned_data.get("weekdays", [])]
        item.save(); return item


class StaffSkillForm(forms.ModelForm):
    class Meta: model = StaffSkill; fields = ["staff", "work_type", "level"]
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = Staff.objects.filter(company=company, active=True) if company else Staff.objects.none()
        self.fields["work_type"].queryset = WorkType.objects.filter(company=company, active=True) if company else WorkType.objects.none()
        self.fields["level"].queryset = SkillLevel.objects.filter(company=company) if company else SkillLevel.objects.none()


class GenerateForm(forms.Form):
    month = forms.DateField(label="対象月", widget=MonthInput(format="%Y-%m"), input_formats=["%Y-%m"])


class ImportForm(forms.Form):
    file = forms.FileField(label="スキル表ファイル", help_text=".xlsx / .xls / .csv")
    def clean_file(self):
        file = self.cleaned_data["file"]
        if not file.name.lower().endswith((".xlsx", ".xls", ".csv")):
            raise forms.ValidationError(".xlsx、.xls、.csvのいずれかを選択してください。")
        return file
