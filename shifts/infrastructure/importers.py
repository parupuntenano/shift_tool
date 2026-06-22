import csv
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook

from shifts.domain.import_data import ImportedSkillMap, ImportedStaffRow


class SkillMapReadError(ValueError):
    pass


class SkillMapFileReader:
    """xlsx/xls/csvを共通のDomain取込データへ変換する。"""

    def read(self, filename: str, file_obj) -> ImportedSkillMap:
        extension = Path(filename).suffix.lower()
        raw = file_obj.read()
        if extension == ".xlsx":
            rows = self._read_xlsx(raw)
        elif extension == ".xls":
            rows = self._read_xls(raw)
        elif extension == ".csv":
            rows = self._read_csv(raw)
        else:
            raise SkillMapReadError(".xlsx、.xls、.csvのいずれかを選択してください。")
        return self._convert(rows)

    @staticmethod
    def _read_xlsx(raw):
        try:
            sheet = load_workbook(BytesIO(raw), data_only=True, read_only=True).active
            return [list(row) for row in sheet.iter_rows(values_only=True)]
        except Exception as exc:
            raise SkillMapReadError("Excelファイルを読み込めませんでした。") from exc

    @staticmethod
    def _read_xls(raw):
        try:
            import xlrd
            sheet = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
            return [sheet.row_values(index) for index in range(sheet.nrows)]
        except Exception as exc:
            raise SkillMapReadError("旧形式Excelファイルを読み込めませんでした。") from exc

    @staticmethod
    def _read_csv(raw):
        for encoding in ("utf-8-sig", "cp932"):
            try:
                return list(csv.reader(StringIO(raw.decode(encoding))))
            except UnicodeDecodeError:
                continue
        raise SkillMapReadError("CSVの文字コードを判定できませんでした。")

    @staticmethod
    def _convert(rows):
        if not rows:
            return ImportedSkillMap(())
        headers = [str(value or "").strip() for value in rows[0]]
        required = {"社員番号", "氏名"}
        if not required.issubset(headers):
            raise SkillMapReadError("見出しに「社員番号」と「氏名」が必要です。")
        employee_index, name_index = headers.index("社員番号"), headers.index("氏名")
        note_index = headers.index("備考") if "備考" in headers else None
        work_columns = [(index, header) for index, header in enumerate(headers) if header and header not in {"社員番号", "氏名", "備考"}]
        result = []
        for raw_row in rows[1:]:
            row = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
            employee_number, name = str(row[employee_index] or "").strip(), str(row[name_index] or "").strip()
            if not employee_number and not name:
                continue
            if not employee_number or not name:
                raise SkillMapReadError("社員番号または氏名が空の行があります。")
            skills = {work: str(row[index] or "").strip() for index, work in work_columns if str(row[index] or "").strip()}
            note = str(row[note_index] or "").strip() if note_index is not None else ""
            result.append(ImportedStaffRow(employee_number, name, note, skills))
        return ImportedSkillMap(tuple(result))
