# バイトジャッジ（Byte Judge）

**闇バイトかどうか、自分では見抜きにくい求人を、LLM が常識的な観点から危険度評価する Web アプリです。**

求人の**テキスト**を貼り付けるだけで、Claude が「闇バイト（特殊詐欺・強盗・口座やカードの受け渡しなどの犯罪加担）」の兆候を評価し、危険度スコアと根拠つきの判定を返します。主なターゲットユーザーは、闇バイトの標的になりやすい 10〜20 代前半の若年層です。

> ⚠️ **免責**：本ツールの判定は LLM による**参考情報**であり、法的・最終的な判断ではありません。「危険な兆候なし」と表示された場合でも危険な求人である可能性は残ります。少しでも不安を感じたら、応募・連絡を行う前に、警察相談専用電話 **#9110** や消費生活センター **188（いやや）** などの正規の窓口に相談してください。

---

## 主な機能

- **貼り付けるだけ**：SNS・求人サイトの募集文をそのまま貼り付け
- **危険度スコア（0〜100）と3段階ラベル**：`危険` / `要注意` / `危険な兆候なし`
- **根拠つきシグナル表示**：「異常な高額報酬」「秘匿アプリへの誘導」など、検出した兆候ごとに深刻度（高/中/低）と判断根拠を提示
- **推奨アクションの提示**：ユーザーが次に取るべき行動をわかりやすい「ですます調」で案内
- **構造化出力**：Claude の structured outputs（Pydantic スキーマ）で JSON を強制し、パース失敗時は結果を表示せずエラー処理


## 判定の観点（一例）

プロンプトで以下のような危険シグナルを評価します。

- 相場からかけ離れた高額報酬（「日給5万円・即日手渡し」など）
- 「即日」「日払い」「誰でも」「簡単」「ノーリスク」等の過度な強調
- 仕事内容が曖昧（「荷物を受け取るだけ」「口座を貸すだけ」など）
- Telegram・Signal など秘匿アプリ／オープンチャットへの誘導
- 身分証や業務に不要な個人情報の早期要求
- 犯罪関連の隠語（「受け子」「出し子」「叩き」「ホワイト案件」など）
- 事業者情報（会社名・所在地・電話番号・許可番号）の欠如や不透明さ

## 使用している LLM

- **Anthropic Claude** — `anthropic` SDK 経由
- モデル名は環境変数 `CLAUDE_MODEL` で切り替え可能（既定：`claude-sonnet-5`。
  `claude-haiku-4-5` は見落としが確認されたため非推奨）
- 出力は `RiskReportSchema`（Pydantic）で構造化
- 安全機構による拒否（`stop_reason: "refusal"`）や出力打ち切り（`max_tokens`）を検出し、その場合は判定結果を表示しません
- レート制限・月額の利用上限・API 障害・拒否などで判定を受けられないときは、その旨と相談先（#9110）を画面に案内します


## 技術スタック

| 領域 | 使用技術 |
|---|---|
| 言語 | Python 3.13 |
| フレームワーク | Django 6.1 |
| LLM | Anthropic Claude（`anthropic`） |
| バリデーション | Pydantic 2 |
| 画像処理 | Pillow |
| 設定管理 | django-environ（`.env`） |
| データベース | 開発は SQLite / 本番は PostgreSQL。保存するのは1日の判定件数のみ |
| 実行環境 | 開発は `runserver` / 本番は gunicorn |
| フロントエンド | Django テンプレート + Bootstrap |
| パッケージ管理 | uv |

---

## セットアップ

### 1. 依存関係のインストール

[uv](https://docs.astral.sh/uv/) を使用します。

```bash
uv sync
```

### 2. 環境変数の設定

プロジェクト直下に `.env` を作成します（`.env` は `.gitignore` 済み。コミットしないでください）。

```dotenv
# Django
SECRET_KEY=<Django のシークレットキー>
ALLOWED_HOSTS=127.0.0.1,localhost

# ローカル開発時のみ、次の行を追加してください（本番では絶対に追加しない）
DEBUG=True

# Anthropic (Claude)
ANTHROPIC_API_KEY=<Claude Console で取得した API キー>
CLAUDE_MODEL=claude-sonnet-5

# LLM を叩かず固定サンプルを返す配線テストモード（任意・既定 False）
VIEW_TEST_MODE=False

# 判定回数の上限（任意・既定は 1人4件 / 全体11件）
PERSON_DAILY_LIMIT=4
SITE_DAILY_LIMIT=11
```

- `SECRET_KEY` は下記で生成できます：
  ```bash
  uv run python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
  ```
- `ANTHROPIC_API_KEY` は [Claude Console](https://platform.claude.com/) で発行してください。
- `CLAUDE_MODEL` は既定の `claude-sonnet-5` を推奨します。
  **`claude-haiku-4-5`（最安）は非推奨です。** 2026-09-22 に実施した
  サンプルデータでの比較テストで、危険な求人の見落としが 1 件ありました
  （Sonnet 5 は 0 件）。費用より見落としの少なさを優先してください。
  また **`claude-fable-5` / `claude-mythos-5` は使用しないでください**（後述）。
- `ALLOWED_HOSTS` は本番環境では実際のドメインに変更してください（例：`ALLOWED_HOSTS=example.com,www.example.com`）。
- `DEBUG=True` は、ローカル開発では入れてください。未設定だと本番扱いになり、
  `DATABASE_URL` の明示が必須になるため、次の手順の `manage.py migrate` が
  `ImproperlyConfigured` で止まります（本番での設定漏れを防ぐガードです。
  「本番環境にデプロイする場合の必須設定」の 3 を参照）。
- `PERSON_DAILY_LIMIT` は 1人あたり、`SITE_DAILY_LIMIT` はサイト全体の、1日の判定
  回数です。前者は連打への摩擦（Cookie で数えるため、消せば回避できます）、後者は
  月額の利用上限を1日で使い切られないための枠です。
  1件あたりの費用は、固定の判定プロンプト（約6,300トークン）がプロンプト
  キャッシュに当たるかどうかで2倍以上変わります。実測は $0.019（連続実行・
  ほぼ全部命中）〜 $0.041（毎回ミス）で、既定の 11件/日 は毎回ミスする最悪の
  想定でも31日で約 $14 に収まる値です。実際の命中率は起動中に出る
  `token_usage` ログ（`cache_read` と `cache_write`）で測れます。
  ただしこの実測は、ふだんの長さの募集文でのものです。入力の上限は10,000文字
  （`apps/judge/forms.py` の `TEXT_MAX_LENGTH`）なので、求人サイトのページを
  雛形ごと貼り付けるなどして上限いっぱいが続くと、その入力ぶん（目安で1万
  トークン＝$0.02 程度）が上乗せされ、1件あたりの費用は上振れします。
  契約している月額上限に合わせて調整してください。
### 3. データベースの初期化と起動

```bash
uv run python manage.py migrate
uv run python manage.py runserver
```

ブラウザで `http://127.0.0.1:8000/` を開くと、求人テキストの入力画面が表示されます。

---

## VIEW_TEST_MODE（配線テストモード）

`VIEW_TEST_MODE=True` にすると、**実際に Claude API を呼び出さず**、固定サンプル（`apps/judge/fixtures.py`）からランダムに判定結果を返します。UI や画面遷移の確認に使えます。

このモード時は画面に「LLM による判定を中止しています。表示される判定結果は使用しないでください。」という警告が表示されます。

---

## プロジェクト構成

```
apps/judge/
├── views.py          # 入力フォームと結果表示（FormView）
├── forms.py          # 画像サイズ/解像度の検証（decompression bomb 対策含む）
├── service.py        # Claude API 呼び出し・プロンプト整形・結果パース
├── schema.py         # RiskReportSchema（score / level / signals / advice / has_enough_info / missing_info）
├── quota.py          # 判定回数の上限（1人＝署名付きCookie / 全体＝日次カウンタ）
├── models.py         # DailyUsage（日付と件数だけの日次カウンタ）
├── fixtures.py       # VIEW_TEST_MODE 用の固定サンプル
├── tests/            # 回帰テスト（守っている約束ごとに分割。全体像は tests/__init__.py）
├── templates/judge/
│   ├── index.html
│   └── prompts/job_offer_risk_assess.md   # 闇バイト判定プロンプト
config/               # Django プロジェクト設定
```

## 入力の制限

- 求人テキストの入力は必須
- 判定できる回数には上限があります
  - **1人あたり 1日 4件**（既定値。`PERSON_DAILY_LIMIT` で変更可。ブラウザの
    Cookie で判定し、日本時間の0時にリセット）
  - **サイト全体で 1日 11件**（既定値。`SITE_DAILY_LIMIT` で変更可）

  どちらに達した場合も判定は行わず、その旨と相談先（#9110）を画面に案内します。

> **注記：画像（スクリーンショット）入力は現在停止中です。**
> UI を `apps/judge/templates/judge/index.html` で `{% comment %}` によりコメントアウトしています。
> バックエンド（`forms.py` の画像検証、`views.py` の画像処理、`tests/` の対応テスト）は
> そのまま残しており、運用方針次第で再開予定です。画像の制限値（最大 5MB / 約 33MP）も
> コード上は有効なままです。

---

## データの取り扱い

### 本アプリのサーバー側に保存しないこと

以下は、コードを読んだだけの説明ではなく、**自動テストで検証**しています
（`apps/judge/tests/`。GitHub Actions で毎回実行されます）。

- **データベースに保存するのは、1日の判定件数だけです。** サイト全体の上限
  （既定 1日11件）を数えるため、`日付` と `件数` の 2 列だけを持つテーブルがあります
  （`apps/judge/models.py` の `DailyUsage`）。求人テキスト・判定結果・利用者を
  特定できる情報は保存せず、それらを保存するテーブルもモデルもありません。
- **セッションに保存しません。** 入力内容も判定結果もセッションに書き込まず、
  セッション自体が作られないことを確認しています。
- **ディスクに書き出しません。** 入力内容はメモリ上だけで処理し、
  一時ファイルも作りません。
- **ログに残しません。** エラーが起きたときのログにも、送信された求人の文面は
  出力されません。
- **レート制限のカウントも、サーバーには保存しません。** 1日あたりの判定回数
  （既定で1人 4件・日本時間の0時にリセット）は、ブラウザ側の Cookie に「日付と件数」
  だけを署名付きで持たせて数えています。入力内容も判定結果も含みません。
  Cookie を消したり別のブラウザを使えば回避できるため、これは連打や軽い荒らしへの
  摩擦です。費用の歯止めは、サイト全体の日次上限（既定 1日11件）と、Anthropic 側の
  月額上限の 2 段で受けています。

処理はリクエストの間だけメモリ上で行われ、画面に結果を表示した時点で破棄されます。

### 本アプリでは防ぎきれないこと

- **ブラウザの履歴には残ります。** 判定結果のページは、ブラウザやプロキシに保存されない
  よう指定しています（`Cache-Control: no-store`）。ただしこの指定で防げるのは
  「キャッシュへの保存」までで、**閲覧履歴は消えません。** また「戻る」ボタンで結果が
  再表示されるかどうかはブラウザによって異なり、必ず防げるとは限りません。
  なお判定結果は URL に含まれないため、履歴に残るのはページを開いた事実だけです。
  共用のパソコンを使っている場合は、確認が終わったら履歴を消してください。
- **回数を数える Cookie が端末に残ります。** 1日の判定回数を数えるため、日付と件数
  だけを持つ Cookie（`bj_quota`）がブラウザに保存されます。入力内容も判定結果も
  含みませんが、「このブラウザで今日この判定を何回使ったか」は残ります。翌0時に
  失効し、ブラウザの設定から削除もできます。共用のパソコンを使っている場合は、
  履歴とあわせて消してください。
- **サーバーを動かしている環境そのものは、本アプリの管理外です。** 上に書いたのは
  「本アプリのコードが何を保存しないか」であって、アプリを動かしているサーバーや
  その前段の設定まで保証するものではありません。ご自身で設置する方は、下の
  「セルフホスト・再配布される方へ」も必ずお読みください。

### LLM API（Anthropic）側

判定のため、入力内容は外部の LLM API（**Anthropic Claude API**）に送信されます。
**ここから先は本アプリの管理外です。** 現時点で Zero Data Retention（ZDR＝データを
一切保持しない契約）は締結していません。通常の商用 API 利用における Anthropic 側の
取り扱いは以下の通りです。

- 送信されたプロンプト・レスポンスが、明示的な許可なく **Anthropic のモデル学習に
  使われることはありません。**
- 送信内容は Anthropic 側で **30日以内に自動削除されます。** 送信直後に消えるわけでは
  ない点にご注意ください。
- **ただし、自動検知システムが利用ポリシー違反としてフラグを立てた場合、または法令上の
  要請がある場合は、上記の30日では消えません。** その場合、
  **入力内容と出力は最大2年間**、**判定に使われた分類スコア（trust and safety
  classification scores）は最大7年間** 保持されることがあります。

#### ⚠️ 本アプリは、フラグが立つ可能性が構造的に高いアプリです

これは軽く読み流さないでください。本アプリが Anthropic に送るのは、
**「犯罪への加担を勧誘している疑いのある文面」そのもの**です。詐欺・強盗・
口座やカードの受け渡し・犯罪の隠語といった言葉が、そのまま外部に送信されます。

そのため、自動検知システムが「利用ポリシー違反かもしれない」と判断する可能性は、
ふつうのアプリより高くなると考えられます。フラグが立った場合、あなたが送った求人の
文面は最大2年間、分類スコアは最大7年間、Anthropic 側に残ります。

**実際にどのくらいの頻度でフラグが立つのかは、本アプリ側からは確認できません。**
「まず起きない」とは言えません。**送った内容が7年間残る可能性がある前提で**
ご利用ください。

#### キャッシュについて

- 本アプリが明示的にキャッシュを指定しているのは、**固定の判定プロンプトだけ**です。
  ユーザーが入力した求人テキストをキャッシュ対象に指定してはいません。
- **これに加えて、Anthropic 側で JSON スキーマ（出力の型定義）が最終使用から
  最大24時間キャッシュされます。** 本アプリは構造化出力（structured outputs＝
  出力の形式を固定する仕組み）を使っているためです。キャッシュされるのは
  「危険度」「レベル」といった**項目名の定義だけ**で、**ユーザーが入力した内容は
  含まれません。**
- Anthropic のドキュメントでは、プロンプトキャッシュ自体についても、プロンプトと出力は
  保存されず、キャッシュの内部表現は保持期間のあいだメモリ上にあり期限切れ後に速やかに
  削除される、と説明されています。

---

## 本番環境にデプロイする場合の必須設定

ローカルで動かすだけなら不要です。

### 1. `DEBUG` を有効にしない

`config/settings.py` は、環境変数 `DEBUG` が未設定のときは `False` に
フォールバックします。**本番環境の `.env` には `DEBUG` の行を書かないでください。**

`DEBUG=True` のまま公開すると、サーバー内部でエラーが起きたときに、詳細な
エラーページがリクエストした相手にそのまま返ります。このページには設定値・
リクエストヘッダ・Cookie などが含まれます。

なお、求人テキストなど POST された内容自体は `sensitive_post_parameters` /
`sensitive_variables` によりマスクされます（`apps/judge/views.py`、
`apps/judge/service.py`）。ただしそれ以外の情報は表示されるため、
`DEBUG` を有効にしないことは依然として必須です。

### 2. `ALLOWED_HOSTS` を設定する

環境変数 `ALLOWED_HOSTS` に、公開するドメインをカンマ区切りで設定してください。

```dotenv
ALLOWED_HOSTS=example.com,www.example.com
```

未設定のまま `DEBUG=False` で起動すると、Django はすべてのリクエストを
拒否します（`DisallowedHost`）。

⚠️ **`runserver` は起動時にエラーで止まりますが、gunicorn などの本番用
サーバーではこのチェックが走りません。**設定漏れに気づくのがデプロイ後の
最初のリクエストになるため、起動前に必ず確認してください。

### 3. データベースを設定する（`DATABASE_URL`）

`DATABASE_URL` には PostgreSQL と SQLite のどちらも指定できます（どちらの形式も
受け付けることは `apps/judge/tests/test_deployment_settings.py` で固定しています）。
開発環境（`DEBUG=True`）で未設定のときは、手元の SQLite にフォールバックします。

```dotenv
# PostgreSQL の例
DATABASE_URL=postgresql://<user>:<password>@<host>/<dbname>?sslmode=require

# SQLite の例（永続ディスク上のパスを指定してください）
DATABASE_URL=sqlite:////data/db.sqlite3
```

⚠️ **本番環境（`DEBUG=False`）では、`DATABASE_URL` の明示的な設定が必須です。**
未設定のまま起動しようとすると `ImproperlyConfigured` で止まります
（`config/settings.py`）。開発用 SQLite への暗黙のフォールバックは行いません。

設定漏れは、起動時に止めます。永続ディスクの無い環境（Cloud Run など）では、
フォールバック先の SQLite にも例外を出さずに書けてしまうためです。その状態で
動くと `DailyUsage` がインスタンスごと・再起動ごとに分かれ、`SITE_DAILY_LIMIT`
が意味を成しません。

永続ディスクを持つ環境（Fly.io のボリュームなど）で SQLite を使う場合は、パスを
明示してください。止めているのは「未設定」だけであって、SQLite の使用そのもの
ではありません。

- 保存するのは `DailyUsage`（日付と件数）だけなので、移行の負担はありません。
- SQLite は書き込み時にデータベース全体をロックするため、gunicorn などで
  ワーカーを複数立てる構成では、書き込みの競合で待ちやエラーが出ます。サイト
  全体の枠の確保は条件付き UPDATE 1文で行っており（`apps/judge/quota.py` の
  `reserve_site_slot`）、PostgreSQL ならこの1文がそのまま上限の保証になります。
- 接続の使い回し（`CONN_MAX_AGE`）は既定の 0（リクエストごとに接続）のままに
  しています。サーバーレス Postgres は無通信が続くとサスペンドし、使い回した
  接続が切れている可能性があること、1リクエストあたりの DB 操作が1行だけで、
  判定そのものにかかる時間に比べれば接続の往復が無視できることによります。
- 接続プーラー（PgBouncer など）を挟む場合も、追加の設定は要りません。Django は
  psycopg 3 のプリペアドステートメントを既定で無効にしています。
- **手元での注意**：`.env` に `DATABASE_URL` を書いたままにすると、
  `manage.py test` がリモートのデータベースにテスト用 DB を作りに行きます。
  普段はコメントアウトし、マイグレーションなど必要なときだけ有効にしてください。

### 4. HTTPS まわりを確認する

前段（ロードバランサや PaaS）が TLS を終端し、アプリには平文の HTTP で渡る
構成を前提にしています。`DEBUG=False` のとき、次の設定が有効になります。

| 設定 | 値 | 役割 |
| --- | --- | --- |
| `SECURE_PROXY_SSL_HEADER` | `X-Forwarded-Proto` を見る | 前段が HTTPS で終端した印を信用する |
| `SECURE_SSL_REDIRECT` | `True` | HTTP で来た接続を HTTPS へ飛ばす |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` | Cookie を HTTPS 限定にする |
| `SECURE_HSTS_SECONDS` | `300` | 5分。運用が安定してから伸ばす |

⚠️ **前段を通さずにアプリへ直接到達できる経路がある構成では、この前提が崩れます。**
`X-Forwarded-Proto: https` を付けるだけで HTTPS だと誤認させられるためです。
その場合は `config/settings.py` の `SECURE_PROXY_SSL_HEADER` を外してください。

`SECURE_SSL_REDIRECT` だけは環境変数で無効にできます（`SECURE_SSL_REDIRECT=False`）。
テストは平文のクライアントで走るため CI ではこれを使っており、本番でリダイレクトが
ループしたときに、イメージを作り直さず環境変数だけで戻せる余地も兼ねています。

デプロイ後に、次の3つを確認してください。

```bash
curl -sI https://<ドメイン>/     # 200 であること。301 ならリダイレクトループ
curl -sI https://<ドメイン>/ | grep -i strict-transport   # HSTS が付くこと
```

加えて、画面から実際に判定を1件送り、**POST が 403 にならないこと**を確認します。
403（CSRF の Origin チェック失敗）は、ブラウザが `https` で送っているのに Django が
自分を `http` だと思っている、つまり `SECURE_PROXY_SSL_HEADER` が効いていない
状態を示します。

HSTS の `includeSubDomains` と `preload` は、どちらも取り消しが効きにくいため
まだ有効にしていません（`manage.py check --deploy` の `W005` / `W021` は、この
判断の結果として残しているものです）。

### 5. その他

- `SECRET_KEY` は本番専用の値を新しく生成してください。開発用の値を
  流用しないでください。
- 判定結果ページは `Cache-Control: no-store` を返します。前段にリバースプロキシや
  CDN を置く場合は、このヘッダが打ち消されたり無視されたりしない設定になっているか
  ご確認ください（「データの取り扱い」の「本アプリでは防ぎきれないこと」を参照）。
- Claude APIのコストと実行回数の閲覧のために、adminを実装予定です。現時点では未実装なので、総当たり攻撃の窓を封鎖するために、`INSTALLED_APPS`に`"django.contrib.admin"`は入れていませんが、将来的な実装の時のため以下は残しています。
  - `INSTALLED_APPS`の `"django.contrib.auth"` / `"django.contrib.contenttypes"`
  - `"django.contrib.auth.middleware.AuthenticationMiddleware"`
  - `"django.contrib.auth.context_processors.auth"`

---

## Google Cloud Run + Neon へのデプロイ

この構成で実際に動くことを確認しています。永続ディスクを持たない代わりに、
使われていないあいだの費用がほぼ出ない組み合わせです。他の PaaS へ移す場合も、
上の「本番環境にデプロイする場合の必須設定」を満たせば同じように動きます。

### 1. Neon（PostgreSQL）

プロジェクトを作成し、接続文字列を2本控えます。ダッシュボードの
Connection string で Connection pooling を切り替えると、両方が得られます。

- **プーリング用**（ホスト名に `-pooler` が入るほう）… アプリが使う
- **直接接続用**… マイグレーションで使う

### 2. Google Cloud の準備

```bash
gcloud projects create <PROJECT_ID> --name="byte-judge"
gcloud config set project <PROJECT_ID>

# 課金アカウントの紐づけ（Cloud Build / Cloud Run に必要）
gcloud billing accounts list
gcloud billing projects link <PROJECT_ID> --billing-account=<ACCOUNT_ID>

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
gcloud config set run/region asia-northeast1
```

### 3. シークレットの登録

`SECRET_KEY` / `ANTHROPIC_API_KEY` / `DATABASE_URL`（プーリング用のほう）を
Secret Manager に置きます。環境変数に直接書くと、サービスの設定を読める人が
そのまま値を見られるためです。

```bash
printf '%s' '<値>' | gcloud secrets create SECRET_KEY --data-file=-
```

⚠️ **`echo` を使わないでください。** 末尾の改行まで値の一部として保存され、
接続文字列やキーが壊れます（`printf '%s'` か、改行を付けないファイルを使う）。

登録したら、実行するサービスアカウントに読み取りを許可します。

```bash
gcloud secrets add-iam-policy-binding SECRET_KEY \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### 4. マイグレーション

Cloud Run 側から流す仕組みは用意していません。**直接接続用**の文字列を使って、
手元から1回実行します。

```bash
DATABASE_URL='<直接接続用の文字列>' uv run python manage.py migrate
```

### 5. デプロイ

`--source .` を指定すると、ビルドは Cloud Build 側で走ります（手元に Docker は
不要です）。`ALLOWED_HOSTS` に入れる URL は
`<サービス名>-<プロジェクト番号>.<リージョン>.run.app` の形になります。

```bash
gcloud run deploy byte-judge --source . --region asia-northeast1 \
  --allow-unauthenticated \
  --memory 512Mi --cpu 1 --concurrency 8 \
  --min-instances 0 --max-instances 4 --timeout 600 \
  --set-env-vars "ALLOWED_HOSTS=<サービスの URL>" \
  --set-secrets "SECRET_KEY=SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,DATABASE_URL=DATABASE_URL:latest"
```

`DEBUG` は渡しません（未設定＝`False`）。2回目以降は、環境変数とシークレットの
指定を省略できます（前のリビジョンから引き継がれます）。

設定値の対応関係は次のとおりです。

| Cloud Run | 対応するもの |
| --- | --- |
| `--concurrency 8` | `Dockerfile` の gunicorn `--threads 8` と揃える |
| `--timeout 600` | 打ち切りの判断はここに一本化（gunicorn 側は `--timeout 0`） |
| `--min-instances 0` | 使われていないあいだは費用が出ない。初回アクセスは5秒ほどかかる |
| `--max-instances 4` | 上振れの歯止め。指定しないと gcloud の既定（20）まで広がる。`SITE_DAILY_LIMIT`（既定 11件/日）に対しては 4 でも十分な余裕がある |

### 6. 確認

```bash
gcloud run services logs read byte-judge --region asia-northeast1 --limit 20
```

起動ログにエラーが無いこと、判定を1件送って `token_usage` の行が出ること、
`DailyUsage` に行が増えることを確認します。費用の見積もりに使う
プロンプトキャッシュの命中率も、この `token_usage` ログ（`cache_read` と
`cache_write`）で測れます。

---

## セルフホスト・再配布される方へ（重要）

- 本アプリは、ユーザーが入力した求人テキストを外部の
  Anthropic Claude API に送信します。これらには個人情報や、犯罪に関わる
  機微な内容が含まれ得ます。
- **必ずご自身で発行した Claude API キー（Commercial 組織のキー）を使用してください。**
  `.env` はリポジトリに含まれていません（`.gitignore` で除外しています）。
  「セットアップ」の手順に従って、ご自身のキーを設定してください。
- **`claude-haiku-4-5` への切り替えは推奨しません。** 2026-09-22 のサンプルデータ
  比較テストで、Sonnet 5 が 0 件だった危険な求人の見落としが 1 件発生しました。
  コスト削減の効果より、見落としによる利用者のリスクのほうが大きいと判断しています。
- **`claude-fable-5` / `claude-mythos-5` は `CLAUDE_MODEL` に設定しないでください。**
  これらは Anthropic の Covered Models に指定されており、**30 日間のデータ保持が必須**で、
  ZDR を適用できません。本アプリの用途にはオーバースペックでもあります。
- **データ保持をさらに短くしたい場合**は、Anthropic のセールスに連絡して
  Zero Data Retention（ZDR）契約を締結してください。ZDR は組織単位で有効化され、
  本アプリが使用する Messages API は適用対象です（Batch・Files API・Managed Agents は対象外）。
  ZDR 適用後も、ポリシー違反としてフラグされた場合や法令上の要請がある場合の保持は残ります。
- ZDR を有効化した組織では **CORS が使用できません**。本アプリは Django のサーバー
  サイドから API を呼び出す構成のため影響はありませんが、フロントエンドから直接
  API を叩く形に改造する場合はご注意ください。
- 上記の「データの取り扱い」の記載は、**ZDR なし**の前提で書かれています。
  運用形態を変更した場合は、この節とあわせて必ず記載を更新してください。
- 本ソフトウェアは無保証で提供されます（ライセンス条項参照）。セルフホスト
  環境での運用・データ管理の責任は、運用者にあります。

---

## コントリビューション

改善の提案を歓迎します。進め方は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

判定の精度に直結するため、**判定プロンプトを変える PR は進め方が他と異なります**
（評価用データセットが非公開のため、メンテナ側で評価を回してから判断します）。
また、**実在の闇バイト求人の文面を issue や PR に貼らないでください**。公開リポジトリに
残ると、そのまま募集文のテンプレートとして使えてしまいます。

## ライセンス

[Apache License 2.0](LICENSE) で公開しています。

商用・非商用を問わず、複製・改変・再配布・セルフホストが可能です。
利用にあたっては、著作権表示とライセンス条項の保持、および改変したファイルの
明示（§4）が必要です。

**本ソフトウェアは「現状有姿」で、明示・黙示を問わずいかなる保証もなく提供されます（§7）。**
判定の誤り（見落とし・誤検知）を含め、利用によって生じた損害について、
著作権者および貢献者は責任を負いません（§8）。セルフホストして提供する場合、
その運用およびデータ管理の責任は、運用する方にあります。
