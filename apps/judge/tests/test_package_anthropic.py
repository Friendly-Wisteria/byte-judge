"""anthropic SDK の更新を検証するためのテスト。

Dependabot が anthropic の更新 PR を作ったときに、こちらのコードを書き換える
必要があるかどうかを判断するために置いている。2段構えになっている。

1. AnthropicSDKSignatureTests — 使用している引数が残っているかを inspect で見る。
   API を叩かないので、更新 PR の CI でそのまま走る（常時実行）。
2. AnthropicAPITests — 実際に API を叩き、今のコードで判定が返るかを見る。
   費用が出るため既定ではスキップし、SDK の検証時に開発環境で手動実行する。

       ANTHROPIC_SDK_TEST=True uv run python manage.py test \
           apps.judge.tests.test_package_anthropic

2 の実行には環境変数 ANTHROPIC_API_KEY が必要（settings ではなく SDK が直接
読む）。モデルは既定で claude-haiku-4-5 を使い、ANTHROPIC_SDK_TEST_MODEL で
差し替えられる。本番モデル固有の破壊を見たいときは、そこに CLAUDE_MODEL と
同じ値を入れる。
"""

import inspect
import logging
import os
import unittest

import anthropic
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from ..schema import Level, MissingInfo, RiskReportSchema, Severity
from ..service import AssessmentError, job_offer_risk_assess

TEST_MODEL = settings.ANTHROPIC_SDK_TEST_MODEL
# 使用中の引数。job_offer_risk_assess の messages.parse 呼び出しと対応する
USED_CREATE_PARAMS = ("model", "max_tokens", "system", "messages", "output_format")

# 検証用の求人文。見たいのは判定の質ではなく「スキーマどおりに返るか」なので、
# どのモデルでも判定がぶれない粗い文面を1行だけ置く。評価用テストセット
# （cases.toml・gitignore 済み）は持ち込まない。募集文のテンプレートとして
# 使えるほど作り込まないこと。
JOB_OFFER_TEXT = (
    "【急募】荷物を受け取るだけの簡単な作業。日給5万円・即日手渡し。"
    "身分証の写真を送れる方。連絡はTelegramで。"
)


class AnthropicSDKSignatureTests(SimpleTestCase):
    def test_anthropic_sdk_has_args_in_current_code(self):
        """
        # テスト
        anthropic SDKに、このプロダクトが使用する範囲で互換性が維持されていること
        ## 前提
        anthropicに対して、inspectを走らせる
        ## 期待する出力
        Anthropic().messages.parseの引数に、
        model, max_tokens, system, messages, output_format が存在する
        ## 必要性
        Dependabotで新しいバージョンが推奨された時の一次テスト
        現在のコードで使用している引数が廃止された場合、実装ごと書き換える必要がある。
        APIを叩かないので、SDKの更新PRのCIでそのまま検出できる。
        ## 限界
        「名前が残っているか」しか見ていない。以下は検出できない。
        * 新たに必須の引数が増えた場合
        * 引数の型やデフォルト値が変わった場合
        * 名前は残ったまま、意味や挙動が変わった場合
        実際にリクエストが通るかは、2段目(AnthropicAPITests)の担当である。
        また、対象は job_offer_risk_assess が使用しているclient.messages.parse のみ
        """
        # 本番コードと同じ到達経路で取る。
        # クラス経由(anthropic.resources.messages.Messages.create)でも取得できるが、
        # 実際に使っている呼び出し方が生きていることまで含めて見たいので、
        # クライアントを構築する。APIキーは不要(構築時に通信しないため)。
        sig = inspect.signature(anthropic.Anthropic().messages.parse)
        for p in USED_CREATE_PARAMS:
            with self.subTest(parameter=p):
                self.assertIn(p, sig.parameters, f"引数 {p} が消えている")

        # どのバージョンで確認したかを記録に残す
        print(f"anthropic version: {anthropic.__version__}")


@unittest.skipUnless(
    settings.ANTHROPIC_SDK_TEST and os.environ.get("ANTHROPIC_API_KEY"),
    "環境変数 ANTHROPIC_SDK_TEST=True が設定されていないためスキップします",
)
@override_settings(VIEW_TEST_MODE=False, CLAUDE_MODEL=TEST_MODEL)
class AnthropicAPITests(SimpleTestCase):
    def test_anthropic_api_response_contains_expected_keys(self):
        """
        # テスト
        SDKを経由して得たレスポンスが、指定したスキーマに沿ってレスポンスを返すこと
        ## 入力
        * 粗い闇バイト文面(JOB_OFFER_TEXT)を job_offer_risk_assess に渡す
        * モデルには TEST_MODEL (既定はHaiku) を指定する。関数はモデルを引数に
          取らないため、override_settings(CLAUDE_MODEL=...) で差し替える
        * VIEW_TEST_MODE は False に固定する。True のままだと API を叩かずに
          fixtures を返すため、テストが空振りする
        ## 期待する出力
        * 戻り値は RiskReportSchema (判定不可を表す AssessmentError ではない)
        * score は 0〜100、level と severity は列挙値、summary と advice は空でない
        * missing_info は列挙値のみで、重複が畳まれている
        * 表示用の computed_field (bs_color / level_label) が引ける
        * 粗い闇バイト文面なので、level は 危険 か 要注意
        # 必要性
        バイトジャッジのコードに変更がなくても、job_offer_risk_assess は
        Anthropic SDKの破壊的変更によって機能しなくなる可能性がある。引数の名前が
        残っていても(1段目が通っても)、意味や既定値が変われば壊れる。そのため、
        今のコードでの挙動を検証するためのテストコードが必要である。
        # 位置づけ
        最小構成のスモークテストであり、切り分けの2段目。
        1段目(AnthropicSDKSignatureTests)との組み合わせが切り分けになる。
        * 両方落ちる -> 引数そのものが廃止された。実装の書き換えが必要
        * こちらだけ落ちる -> 名前は残ったまま意味・既定値・構造化出力の扱いが
          変わった。あるいは通信・認証の問題
        年に数回しか実行しないテストなので、失敗したときに考えずに済む構造を優先する。
        # 運用
        * このテストは、Anthropic SDKのアップデートの検証時のみ実行する。
        * 実行時には、テスト環境の環境変数に`ANTHROPIC_SDK_TEST`を追加し、値をTrueとすること。
        * 1回あたりの費用は、Haikuで約 $0.011(実測: cache_write 6,093 /
          output 598 トークン)。年に数回の手動実行なのでプロンプト
          キャッシュは毎回ミスし、常にこの最悪ケースになる。
        # 対象外
        * 判定の精度・安定性は evaluate_prompt コマンドの担当であり、ここでは見ない
        * モデルの変更による実装の破壊は対象外とする
            * 既定の実行ではHaiku固定のため、本番モデル固有の破壊は拾えない。
              検証したい場合は ANTHROPIC_SDK_TEST_MODEL で差し替える
              (モジュールのdocstringを参照)
        * 7番のアサートだけはモデルの出力内容に依存する。ここだけ落ちた場合は、
          SDKではなくプロンプト側を見る
        """
        with self.assertLogs("apps.judge", level="INFO") as logs:
            result = job_offer_risk_assess(JOB_OFFER_TEXT)

        # --- 1. 判定が返っていること -------------------------------------
        # 判定不可のときは AssessmentError が返る。どの理由かはログに出る。
        self.assertNotIsInstance(
            result, AssessmentError, f"判定を受け取れなかった: {result!r}"
        )
        self.assertIsInstance(result, RiskReportSchema)
        errors = [r.getMessage() for r in logs.records if r.levelno >= logging.ERROR]
        self.assertEqual(errors, [], "ERROR ログが出ている")

        # --- 2. 固定サンプルではなく、実際に API を叩いた結果であること ---
        # service は応答のモデル名を INFO ログに出す。VIEW_TEST_MODE の
        # 取り違えでテストが空振りすることを防ぐ。
        model_lines = [
            m
            for m in (r.getMessage() for r in logs.records)
            if m.startswith("Claude Model:")
        ]
        self.assertEqual(len(model_lines), 1, "API 呼び出しが1回行われていない")
        self.assertIn(TEST_MODEL.split("-")[1], model_lines[0])  # haiku / sonnet ...

        # --- 3. 値域と列挙値 ---------------------------------------------
        # 構造化出力のスキーマには数値・文字列長の制約を渡せず、pydantic が
        # クライアント側で検証している。API 側の保証が無い部分なので実値を見る。
        self.assertIsInstance(result.score, int)
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)
        self.assertIn(result.level, tuple(Level))
        self.assertNotEqual(result.summary.strip(), "")
        self.assertNotEqual(result.advice.strip(), "")
        self.assertIsInstance(result.has_enough_info, bool)

        # --- 4. signals の形 ---------------------------------------------
        for signal in result.signals:
            with self.subTest(signal=signal.name):
                self.assertNotEqual(signal.name.strip(), "")
                self.assertNotEqual(signal.detail.strip(), "")
                self.assertIn(signal.severity, tuple(Severity))

        # --- 5. missing_info は列挙値だけで、重複が畳まれていること -------
        for item in result.missing_info:
            with self.subTest(missing=item):
                self.assertIn(item, tuple(MissingInfo))
        self.assertEqual(len(result.missing_info), len(set(result.missing_info)))

        # --- 6. 表示用の computed_field が引けること ----------------------
        # bs_color は computed_field なので構造化出力のスキーマから除外される。
        # 除外が効かないと、モデルがこの項目を出そうとして生成が壊れる
        # （Gemini からの移行時に実際に起きた）。
        self.assertIn(result.bs_color, ("danger", "warning", "success", "secondary"))
        self.assertNotEqual(result.level_label, "")

        # --- 7. 判定の中身（ここだけモデルの出力内容に依存する） ----------
        # 粗い闇バイト文面なので「危険な兆候なし」で返るのは、スキーマは
        # 満たしていてもプロンプトか渡し方が壊れている。level は
        # has_enough_info と独立に入るため、情報不足で返っても成立する。
        self.assertIn(result.level, (Level.DANGER, Level.CAUTION))
        self.assertTrue(result.signals, "シグナルが1件も挙がっていない")

        # 目視確認用。アサーションは「壊れていないこと」しか見ないので、出力が
        # 想定どおりの体裁か(ですます調・読みやすさ)は人間が読んで判断する。
        # 手動実行前提のテストなので、printは意図的に残している。
        # --buffer を付けると消える。
        print(f"\n{model_lines[0]}")
        print(
            f"score={result.score} level={result.level_label} "
            f"enough={result.has_enough_info} missing={result.missing_info}"
        )
        print(f"summary: {result.summary}")
        print(f"advice : {result.advice}")
        for signal in result.signals:
            print(f"  - [{signal.severity}] {signal.name}: {signal.detail}")
