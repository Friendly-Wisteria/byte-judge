"""判定アプリの回帰テスト。

守っている約束ごとにファイルを分けている。設定値の目視確認では将来の変更で
退行しても気づけないため、実際にリクエストを通して、外部に残る場所や利用者に
見えるものを検査する。

- test_no_stored_input.py       入力がディスク・ログ・セッション・キャッシュに残らないこと
- test_usage_log.py             トークン使用量のログに、取り決めた項目だけが出ること
- test_prompt_injection.py      求人テキストが囲みタグの境界を偽装できないこと
- test_image_input.py           画像の受け口が閉じていること・API に渡す変換
- test_result_display.py        情報不足の扱いと、「安全」と言い切らない表示
- test_schema_values.py         判定JSONの値域と、見本が画面まで通ること
- test_judgment_unavailable.py  判定を返せないときの振り分けと、相談先の案内
- test_view_test_mode.py        配線テストモードの結果を、本物と誤認させないこと
- test_oversized_request.py     リクエストが上限を超えたときの見え方
- test_deployment_settings.py   デプロイ時の前提（CSRF・ホスト名・マイグレーション）
- test_disclosure.py            入力画面の、外部送信の説明
- test_quota.py                 1日の判定回数の上限（個人の Cookie / サイト全体）
- test_evalset_dataset.py       評価用テストセットの読み込み
- test_evalset_metrics.py       評価指標の計算
- test_evalset_command.py       evaluate_prompt コマンドと、その JSON 出力

共有の道具（目印の文字列・ログ収集・SDK 例外の生成）は helpers.py にある。
テストではないので、Django の収集対象（test*.py）から外してある。

テンプレートの JavaScript には、意図してテストを置いていない（#20）。

ブラウザに届いているのは63行で、内訳は「送信中のオーバーレイと二重送信の
防止」「テキストの空送信のブロック」「タブの記憶」「『戻る』で復帰したときの
リセット」。このうち空送信は forms 側に、二重送信は1日の上限（test_quota.py）
に同じ備えがあるため、Python のテストで守られていないのは、復帰時にオーバー
レイを消す処理だけになる。

そのためだけに実ブラウザのバイナリ（0.1GB超）を持ち込んだり、npm を足して
パッケージ管理を二本立てにするのは、釣り合わないと判断した。ここが実際に
問題になった場合は、テストを足すのではなく、ビジー表示をオーバーレイ以外の
方法に変えることで対処する。

画像入力を再開するとき（#26）は、DOM を触る処理が90行ほど戻ってくるため、
この判断は改めて見直す。
"""
