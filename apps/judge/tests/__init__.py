"""判定アプリの回帰テスト。

ファイル名の接頭辞が、分け方の軸を表している。

- test_spec_*     機能横断的に守っている約束ごとに分けたもの。ここが中心
- test_evalset_*  評価セット（メンテナ用の道具）。実装の単位ごとに分けたもの
- test_repo_*     リポジトリと依存の整合。アプリの約束ではない

設定値の目視確認では将来の変更で退行しても気づけないため、実際にリクエストを
通して、外部に残る場所や利用者に見えるものを検査する。

基底クラスは、DB への問い合わせが必要なクラスだけ TestCase にして、ほかは
SimpleTestCase にしている。SimpleTestCase は問い合わせの時点で落ちるため、
「このテストは DB を使わないつもりで書いた」ことが、そのまま仕組みとして効く。

守っている約束:

- test_spec_no_stored_input.py       入力がディスク・ログ・セッション・キャッシュに残らないこと・
                                     使用量のログに入力に由来するものが出ないこと・
                                     数えるために保存するのは日付と件数だけであること
- test_spec_prompt_injection.py      求人テキストが囲みタグの境界を偽装できないこと
- test_spec_result_display.py        情報不足の扱い、「安全」と言い切らない表示、
                                     判定JSONの値域と配色（画面の見え方を崩す値を通さないこと）
- test_spec_judgment_unavailable.py  判定を返せないときの振り分けと、相談先の案内
- test_spec_view_test_mode.py        配線テストの結果を本物と誤認させないこと・見本が全件画面に
                                     出せること・費用も枠も使わないこと
- test_spec_oversized_request.py     リクエストが上限を超えたときの見え方
- test_spec_error_pages.py           差し替えたエラーページ（400 / 403 / 404 / 500）の見え方
- test_spec_deployment_settings.py   デプロイ時の前提（CSRF・ホスト名・マイグレーション・admin）
- test_spec_disclosure.py            入力画面の、外部送信の説明
- test_spec_quota.py                 個人の1日の上限（署名付き Cookie だけで数える）
- test_spec_cost_is_capped.py        費用が想定を超えないこと（サイト全体の枠）・使ったぶんが
                                     ログから追えること

メンテナ用の道具:

- test_evalset_dataset.py            評価用テストセットの読み込み
- test_evalset_metrics.py            評価指標の計算
- test_evalset_command.py            evaluate_prompt コマンドと、その JSON 出力

リポジトリと依存の整合:

- test_repo_release_version.py       pyproject.toml のバージョンと、リリースタグの整合
- test_repo_package_anthropic.py     anthropic SDK の更新で、こちらの手当てが要るかの検知

接頭辞が付いていない1件:

- test_image_input.py                画像入力。停止中の機能なので、再開（#26）まで実装の単位で残す

共有の道具（目印の文字列・ログ収集・SDK 例外の生成）は helpers.py にある。
テストではないので、Django の収集対象（test*.py）から外してある。

テンプレートの JavaScript には、意図してテストを置いていない（#20）。

ブラウザに届いているのは63行で、内訳は「送信中のオーバーレイと二重送信の
防止」「テキストの空送信のブロック」「タブの記憶」「『戻る』で復帰したときの
リセット」。このうち空送信は forms 側に、二重送信は1日の上限（test_spec_quota.py）
に同じ備えがあるため、Python のテストで守られていないのは、復帰時にオーバー
レイを消す処理だけになる。

そのためだけに実ブラウザのバイナリ（0.1GB超）を持ち込んだり、npm を足して
パッケージ管理を二本立てにするのは、釣り合わないと判断した。ここが実際に
問題になった場合は、テストを足すのではなく、ビジー表示をオーバーレイ以外の
方法に変えることで対処する。

画像入力を再開するとき（#26）は、DOM を触る処理が90行ほど戻ってくるため、
この判断は改めて見直す。
"""
