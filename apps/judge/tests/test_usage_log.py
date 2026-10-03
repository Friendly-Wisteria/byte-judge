"""トークン使用量のログが、入力に由来する情報を残さないことの回帰テスト。

費用の把握には使いたいが、ログは残るものなので、入力の長さや判定した時刻の
分秒といった「入力や利用者をたどれるもの」は落としてから出す。記録する項目が
増えていないことも、ここで固定する。

かかった費用をログから追えること自体は test_spec_cost_is_capped.py で見る。
"""

import re
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from .. import service
from ..fixtures import FIXTURES
from ..schema import RiskReportSchema
from .helpers import JOB_TEXT, MARKER, assess_with_usage, usage_line


# このクラスは実 API 経路（使用量が出る経路）を見るため、VIEW_TEST_MODE を
# 明示的に落とす。手元の .env が True だと、fixtures を返す経路に入って
# 使用量のログが1行も出ず、ログを検査するテストがまとめて落ちる。テストの
# 結果が環境変数に左右されないよう、ここで固定する。
@override_settings(VIEW_TEST_MODE=False)
class TokenUsageIsLoggedTests(TestCase):
    """使用量のログに、入力に由来するものが残らないことの検証。

    DB に持つと「保存するのは1日の判定件数だけ」という約束が崩れる。また、
    入力の長さは入力内容に由来する唯一の情報なので記録せず、時刻も時までに
    丸める。記録する項目が増えていないことも、ここで固定する。
    """

    def test_input_tokens_are_rounded_to_a_hundred(self):
        """入力トークン数は、そのまま残さないこと。

        input_tokens は貼り付けられた求人文の長さの近似値になる。判定プロンプトは
        キャッシュされて cache_read 側に回るため、2回目以降はほぼ求人文の分だけに
        なり、入力の長さがそのまま残ってしまう。
        """
        for raw, rounded in [(4821, "4800"), (4850, "4900"), (49, "0"), (150, "200")]:
            with self.subTest(input_tokens=raw):
                line = usage_line(
                    self,
                    assess_with_usage(
                        input_tokens=raw, output_tokens=1, cache_read_input_tokens=0
                    ),
                )
                self.assertIn(f"input_100={rounded}", line)
                self.assertNotIn(str(raw), line)

    def test_timestamp_is_rounded_to_the_hour(self):
        """分・秒を残さないこと（判定した時刻から利用者をたどれないように）。"""
        line = usage_line(
            self,
            assess_with_usage(
                input_tokens=1, output_tokens=1, cache_read_input_tokens=0
            ),
        )

        hour = re.search(r"hour=(\S+)", line).group(1)
        self.assertRegex(hour, r"^\d{4}-\d{2}-\d{2}T\d{2}\+09:00$")

    def test_only_the_agreed_fields_are_recorded(self):
        """入力文字数など、取り決めにない項目が増えていないこと。"""
        line = usage_line(
            self,
            assess_with_usage(
                input_tokens=1234, output_tokens=567, cache_read_input_tokens=890
            ),
        )

        self.assertEqual(
            re.findall(r"(\w+)=", line),
            ["hour", "model", "input_100", "output", "cache_read", "cache_write"],
        )

    def test_the_job_text_never_reaches_the_usage_log(self):
        output = assess_with_usage(
            input_tokens=1234, output_tokens=567, cache_read_input_tokens=890
        )

        self.assertIn("token_usage", output)
        self.assertNotIn(MARKER, output)
        self.assertNotIn(str(len(JOB_TEXT)), usage_line(self, output))

    def test_a_failing_usage_log_does_not_change_the_verdict(self):
        """使用量のログで例外が出ても、判定の結果を捨てないこと。

        ログの組み立ては API 呼び出しの try の中にある。ここで投げると
        API 障害として扱われ、受け取れていた判定が失われる。
        """

        class ExplodingUsage:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        response = mock.Mock(
            stop_reason="end_turn",
            model="claude-sonnet-5",
            parsed_output=RiskReportSchema.model_validate(FIXTURES["danger"]),
            usage=ExplodingUsage(),
        )
        with mock.patch.object(service.anthropic, "Anthropic") as client_class:
            client_class.return_value.messages.parse.return_value = response
            result = service.job_offer_risk_assess(JOB_TEXT)

        self.assertIsInstance(result, RiskReportSchema)

    def test_usage_format_carries_no_precise_timestamp(self):
        """書式が秒までの時刻を足すと、丸めた意味が無くなる。"""
        logger_conf = settings.LOGGING["loggers"]["apps.judge.usage"]
        handler = settings.LOGGING["handlers"][logger_conf["handlers"][0]]
        fmt = settings.LOGGING["formatters"][handler["formatter"]]["format"]

        self.assertNotIn("asctime", fmt)

    def test_nothing_is_written_to_the_database(self):
        """使用量のために、保存するモデルが増えていないこと。"""
        from django.apps import apps as django_apps

        models = {m.__name__ for m in django_apps.get_app_config("judge").get_models()}

        self.assertEqual(models, {"DailyUsage"})
