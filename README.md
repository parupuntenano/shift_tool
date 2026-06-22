# ShiftFlow — 汎用シフト自動作成システム

企業ごとに異なる業務名、スキル記号、勤務ルールを設定データとして管理し、月間シフトを自動生成するDjangoアプリです。現在は仕様書のフェーズ1を実装しています。

## セットアップ

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

[http://127.0.0.1:8000/](http://127.0.0.1:8000/) を開きます。

デモログイン：

- 管理者：`admin` / `admin123`
- スタッフ：`staff1` / `staff123`

## フェーズ1の実装範囲

- Django Authenticationによるログイン
- 管理者・スタッフ別の固定サイドバー
- マルチ企業対応のデータモデル
- スタッフ、業務、スキル区分、スタッフスキル、個別制約の管理
- `.xlsx` / `.xls` / `.csv` スキルマップ取込
- スタッフによる月次の勤務可能日・希望休・勤務不可日提出
- スキル優先度、提出内容、最大連続勤務日数を考慮した自動生成
- 警告表示、公開、スタッフ本人のシフト確認

## スキル表の形式

1行目を見出しとして、次の形式を利用します。

```text
社員番号,氏名,備考,受付,検品,出荷
S001,青木 花,4勤不可,◎,○,×
```

`社員番号` と `氏名` は必須です。それ以外の列は業務名として取り込まれ、各セルの値がスキル記号になります。未登録の業務・記号も企業のマスタとして作成されます。

## アーキテクチャ

```text
Presentation → Application → Domain
       ↓              ↑
Infrastructure ───────┘
```

- `shifts/domain/`: 純粋Pythonの業務ルール・生成器。Django非依存
- `shifts/application/`: ユースケースとインターフェース
- `shifts/infrastructure/`: Django ORM、ファイル読込、Repository
- `shifts/presentation/`: Views、Forms、Templates

## テスト

```powershell
python manage.py test
```

DomainテストはWeb画面やDBを起動せずに実行できます。

## 今後のフェーズ

- フェーズ2：急な休み、自動再配置、Excel/CSV出力
- フェーズ3：最適化、評価、過去分析、品質スコア
