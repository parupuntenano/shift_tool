# ShiftFlow — 30人ローカル版 月間シフト作成システム

30人規模の現場向けに、Excel情報を読み込んで管理者が使える月間シフト表を作るDjangoアプリです。

目的は「完璧な全自動」ではなく、管理者が手直ししやすい8割完成の下書きを作ることです。必要人数、公休数、スキル不可、個別制約、前月実績を見ながらシフトを自動作成し、最後は管理者が編集・出力します。

## 主な機能

- 管理者ログイン
- スタッフ、業務、スキル記号、スタッフスキル、個別制約の管理
- Excel / CSVによるスタッフ・スキル・業務・前月シフト実績の取込
- 取込用テンプレート、サンプルファイルのダウンロード
- 祝日APIを使った祝日名表示、土日祝の色分け
- 管理者によるシフト自動生成、編集、保存
- 制約違反、必要人数、公休数、月日数合計の警告表示
- 研修者と監督者のマンツーマン配置補助
- シフト表のExcel / CSVダウンロード

## 完成優先フェーズで扱わない機能

以下は将来オプション扱いです。

- スタッフログイン
- スタッフのWebシフト提出
- スタッフのシフト確認
- 急休申請
- 未提出者一覧
- メール送信
- パスワード忘れ
- スマホ専用表示
- クラウド運用前提の機能

## セットアップ

Windows PowerShell例です。

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

## Excel / CSV取込の基本

取込ページからテンプレートとサンプルをダウンロードできます。実運用では、まずスタッフ・業務・スキルを取り込み、その後に必要に応じて前月シフト実績を取り込みます。

スキル表は1行目を見出しとして、次の形式を使います。

```text
社員番号,氏名,備考,受付,検品,出荷
S001,スタッフ01,4勤不可,◎,○,×
```

- `社員番号` と `氏名` は必須です。
- `備考` は勤務ルール候補として利用します。
- 業務列のセルに入った記号は、会社ごとのスキル記号として自動登録されます。
- 前月シフト実績は別シート/別取込として、月末1週間分を読み取ります。

詳しい完成仕様は [docs/local_30_shift_spec.md](docs/local_30_shift_spec.md) を参照してください。

## アーキテクチャ

```text
Presentation → Application → Domain
       ↓              ↑
Infrastructure ───────┘
```

- `shifts/domain/`: Djangoに依存しない業務ルール・生成ロジック
- `shifts/application/`: ユースケース、インターフェース、画面に依存しないアプリケーション処理
- `shifts/infrastructure/`: Django ORM、Excel/CSV読込、Repository
- `shifts/presentation/`: Views、Forms、Templates
- `static/`: 共通CSS
- `templates/`: 画面HTML

依存の向きは、できるだけ外側から内側へ寄せます。

- `domain` は Django / Excel / HTML を知らない。
- `application` は `domain` を使って業務処理を組み立てる。
- `infrastructure` は DB や Excel など外部入出力を担当する。
- `presentation` は request / response / template の組み立てに集中する。

例：シフト生成ロジックは `shifts/domain/` に寄せ、画面側は入力値の受け渡しと表示に集中させます。

## テスト

```powershell
python manage.py test
```

設定確認だけを行う場合：

```powershell
python manage.py check
```

## ローカル運用の前提

- 管理者PCで起動して使うローカル運用を主対象にします。
- 管理者以外が直接シフト作成・編集を行わない想定です。
- 同梱サンプルは架空の `スタッフ01` 形式のデータです。実在する個人情報を含むサンプルデータを同梱しないでください。

## 将来、本番・クラウド運用する場合の前提

- HTTPS環境で運用してください。
- 本番環境では `DJANGO_DEBUG=0` を設定してください。
- `DJANGO_SECRET_KEY`、DBパスワードなどの秘匿情報は環境変数で管理してください。
- `DJANGO_ALLOWED_HOSTS` には本番ドメインのみを設定してください。
- DBは外部ネットワークから直接アクセスできない構成にしてください。

### 本番用の主な環境変数

`.env.production.example` を基準に設定してください。

```text
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_ALLOWED_HOSTS=example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://example.com

DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
DJANGO_SECURE_HSTS_SECONDS=31536000
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1
DJANGO_SECURE_HSTS_PRELOAD=1
```

SQLite以外のDBを使う場合：

```text
DB_ENGINE=django.db.backends.postgresql
DB_NAME=shiftflow
DB_USER=shiftflow
DB_PASSWORD=replace-with-db-password
DB_HOST=127.0.0.1
DB_PORT=5432
DB_CONN_MAX_AGE=60
```

HTTPS終端がリバースプロキシの場合は、必要に応じて設定します。

```text
DJANGO_SECURE_PROXY_SSL_HEADER=1
```

### ローカルで本番相当モードを試す

ローカルHTTPで確認するため、HTTPS関連だけ一時的に無効化する例です。本番では無効化しないでください。

```powershell
$env:DJANGO_DEBUG="0"
$env:DJANGO_SECRET_KEY="local-production-check-secret-change-me"
$env:DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost"

$env:DJANGO_SECURE_SSL_REDIRECT="0"
$env:DJANGO_SESSION_COOKIE_SECURE="0"
$env:DJANGO_CSRF_COOKIE_SECURE="0"
$env:DJANGO_SECURE_HSTS_SECONDS="0"

python manage.py migrate
python manage.py collectstatic --noinput
waitress-serve --listen=127.0.0.1:8001 config.wsgi:application
```

## 運用メモ

- `DEBUG=False` の変更はサーバー再起動後に反映されます。
- 静的ファイルを変更した場合は、本番相当モードでは `collectstatic` を再実行してください。
- Excel取込前に、サンプルファイルで列名と記入例を確認してください。
- 本番DBのバックアップは、Excel取込や一括変更の前に取得する運用を推奨します。
