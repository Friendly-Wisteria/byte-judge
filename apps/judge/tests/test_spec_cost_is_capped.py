"""費用が想定を超えないこと、使ったぶんが追えることの回帰テスト。

個人の枠（Cookie）は消せば戻るので、費用の歯止めはサイト全体の枠に置いている。
その日のうちに止まること・止まったあとは API を叩かないこと・ワーカーが複数でも
超えないことを見る。あわせて、かかった費用をログから追えることを見る。

断るときの案内の文言そのものは test_spec_judgment_unavailable.py が本体。
ここでは「枠が尽きた経路からあの案内に届く」ことだけを見る。
"""

from datetime import datetime, timedelta
from unittest import mock

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .. import quota, views
from ..fixtures import FIXTURES
from ..models import DailyUsage
from ..schema import RiskReportSchema
from .helpers import (
    JOB_TEXT,
    assert_consultation_is_offered,
    assess_with_usage,
    usage_line,
)


@override_settings(VIEW_TEST_MODE=False, SITE_DAILY_LIMIT=2)
class SiteDailyLimitTests(TestCase):
    """サイト全体の1日の上限の検証。

    個人の枠（Cookie）は消せば戻るため、月額の利用上限を1日で使い切られる
    経路が残る。全体の枠でその日のうちに止まること、止まったあとは API を
    叩かないこと（＝費用が出ないこと）を見る。

    保存するものが日付と件数だけであることは test_spec_no_stored_input.py、
    配線テストモードが枠を消費しないことは test_spec_view_test_mode.py で見る。
    """

    def setUp(self):
        self.report = RiskReportSchema.model_validate(FIXTURES["danger"])

    def _judge(self, client=None):
        with mock.patch.object(
            views, "job_offer_risk_assess", return_value=self.report
        ) as assess:
            response = (client or self.client).post(
                "/", {"mode": "text", "text": JOB_TEXT}
            )
        return response, assess

    def test_limit_applies_across_visitors(self):
        """枠が尽きたら、Cookie を持たない別の利用者でも断られること。"""
        for i in range(settings.SITE_DAILY_LIMIT):
            with self.subTest(nth=i + 1):
                response, _ = self._judge(Client())
                self.assertContains(response, "危険度")

        response, assess = self._judge(Client())

        self.assertIsNone(response.context.get("result"))
        self.assertContains(response, "あなたの使いすぎではありません")
        assert_consultation_is_offered(self, response)
        # 枠を取れなかった判定は API に届かない（＝費用が出ない）
        assess.assert_not_called()

    def test_count_never_exceeds_the_limit(self):
        """確保に失敗した回数ぶん、件数が増えていないこと。"""
        for _ in range(settings.SITE_DAILY_LIMIT + 3):
            self._judge(Client())

        usage = DailyUsage.objects.get()
        self.assertEqual(usage.count, settings.SITE_DAILY_LIMIT)

    def test_limit_recovers_at_the_jst_date_boundary(self):
        """日本時間で日付を跨ぐと、サイト全体でのAPI利用回数がリセットされること"""
        before = datetime(2026, 8, 18, 23, 59, tzinfo=quota.JST)
        after = datetime(2026, 8, 19, 0, 1, tzinfo=quota.JST)

        with mock.patch.object(quota, "_now_jst", return_value=before):
            for _ in range(settings.SITE_DAILY_LIMIT):
                self._judge(Client())
            response, _ = self._judge(Client())
            self.assertContains(response, "あなたの使いすぎではありません")

        with mock.patch.object(quota, "_now_jst", return_value=after):
            response, _ = self._judge(Client())
            self.assertContains(response, "危険度")

    def test_a_database_error_stops_the_api_call(self):
        """DB 障害のときは、API を叩かずにサイト側の障害として案内すること。

        件数を記録できない状態で投げ続けると、歯止めが無いまま費用だけが出る。
        """
        with mock.patch.object(
            DailyUsage.objects,
            "get_or_create",
            side_effect=DatabaseError("connection lost"),
        ):
            response, assess = self._judge(Client())

        self.assertIsNone(response.context.get("result"))
        self.assertContains(response, "サイト側の問題なので")
        # 上限で埋まったときの案内と混ざっていないこと（原因の取り違えを防ぐ）
        self.assertNotContains(response, "あなたの使いすぎではありません")
        assert_consultation_is_offered(self, response)
        # 枠を取れなかった判定は API に届かない（＝費用が出ない）
        assess.assert_not_called()


@override_settings(SITE_DAILY_LIMIT=2)
class SiteCounterHoldsUnderContentionTests(TestCase):
    """全体の枠を数える処理が、競合と経年で崩れないことの検証。

    ワーカーが複数ある本番では、read してから write する実装だと上限を
    超えて通してしまう（費用の歯止めが外れる）。ただし実際の並行実行は
    SQLite では再現できない（テーブルロックで1件しか通らず、実装を
    入れ替えても緑になる）。そこで、上限の判定が SQL の条件に入っていること
    自体を固定する。
    """

    NOW = datetime(2026, 9, 22, 12, 0, tzinfo=quota.JST)
    TODAY = NOW.date()

    def test_the_reservation_is_one_conditional_update(self):
        """確保が、上限と当日を条件に含む UPDATE 1文で行われること。"""
        quota.reserve_site_slot(self.NOW)  # 当日の行を作る

        with CaptureQueriesContext(connection) as captured:
            is_reserved, failure_reason = quota.reserve_site_slot(self.NOW)
            self.assertTrue(is_reserved)
            self.assertIsNone(failure_reason)

        updates = [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].lstrip().upper().startswith("UPDATE")
        ]

        self.assertEqual(len(updates), 1, "確保が UPDATE 1文になっていない")
        # SET と WHERE を分けてから当てる。文全体を見ると、日付を SET 側に書く
        # 実装（例: save() で全列を書き戻す）でも日付の条件が通ってしまう。
        set_clause, _, where_clause = updates[0].partition("WHERE")
        # 上限と当日の判定が、どちらも WHERE に入っていること
        # （Python 側で読んで比べていない／当日以外の行を増やしていない）。
        # 条件の並び順と識別子の引用は backend 任せなので、条件ごとに別に見る。
        # 日付リテラルは SQLite が '2026-09-22'、PostgreSQL が '2026-09-22'::date
        # と出すため、後ろを縛らない形にしてある。
        self.assertRegex(where_clause, rf'count"?\s*<\s*{settings.SITE_DAILY_LIMIT}')
        self.assertRegex(where_clause, rf"""date"?\s*=\s*'{self.TODAY.isoformat()}'""")
        # 件数の加算も SQL 側でやっていること（read してから write していない）
        self.assertRegex(set_clause, r'count"?\s*=\s*\(?[^)]*count"?\s*\+\s*1')

    def test_another_days_row_is_left_alone(self):
        """当日以外の行を増やしていないこと（確保が当日の行に限られること）。"""
        yesterday = self.TODAY - timedelta(days=1)
        DailyUsage.objects.create(date=yesterday, count=0)

        is_reserved, _ = quota.reserve_site_slot(self.NOW)

        self.assertTrue(is_reserved)
        self.assertEqual(DailyUsage.objects.get(date=yesterday).count, 0)
        self.assertEqual(DailyUsage.objects.get(date=self.TODAY).count, 1)

    def test_a_row_already_at_the_limit_is_not_incremented(self):
        """すでにリミットに達したレコードは、それ以上カウントアップされないこと"""
        DailyUsage.objects.create(date=self.TODAY, count=settings.SITE_DAILY_LIMIT)
        is_reserved, failure_reason = quota.reserve_site_slot(self.NOW)

        self.assertFalse(is_reserved)
        self.assertEqual(
            failure_reason, quota.SiteSlotReservationError.DAILY_QUOTA_REACHED
        )
        self.assertEqual(DailyUsage.objects.get().count, settings.SITE_DAILY_LIMIT)

    def test_a_row_created_by_another_worker_does_not_break_the_reservation(self):
        """同時に行が作られて IntegrityError になっても、確保を続けること。"""
        DailyUsage.objects.create(date=self.TODAY, count=0)

        with mock.patch.object(
            DailyUsage.objects, "get_or_create", side_effect=IntegrityError("race")
        ):
            is_reserved, failure_reason = quota.reserve_site_slot(self.NOW)
            self.assertTrue(is_reserved)
            self.assertIsNone(failure_reason)

        self.assertEqual(DailyUsage.objects.get().count, 1)

    def test_a_database_error_is_reported_as_the_reason(self):
        """DB に触れないときは、確保を諦めて理由を DATABASE_ERROR で返すこと。"""
        with mock.patch.object(
            DailyUsage.objects,
            "get_or_create",
            side_effect=DatabaseError("connection lost"),
        ):
            is_reserved, failure_reason = quota.reserve_site_slot(self.NOW)

        self.assertFalse(is_reserved)
        self.assertEqual(failure_reason, quota.SiteSlotReservationError.DATABASE_ERROR)


# 使用量のログは実 API 経路でしか出ないため、手元の .env に左右されないよう
# VIEW_TEST_MODE を明示的に落とす（fixtures を返す経路ではログが1行も出ない）。
@override_settings(VIEW_TEST_MODE=False)
class TokenUsageIsTraceableTests(SimpleTestCase):
    """使ったぶんを後から追えることの検証。

    費用は請求が来てから分かるのでは遅い。どのモデルで何トークン使い、
    プロンプトキャッシュに当たったかが、ログだけで追えるようにしておく。
    記録する項目を増やさないこと（入力に由来する情報を残さない）は
    test_spec_no_stored_input.py 側で見る。
    """

    def test_model_and_token_counts_are_recorded(self):
        line = usage_line(
            self,
            assess_with_usage(
                input_tokens=1234,
                output_tokens=567,
                cache_read_input_tokens=890,
                cache_creation_input_tokens=432,
            ),
        )

        self.assertIn("model=claude-sonnet-5", line)
        self.assertIn("input_100=1200", line)  # 1234 を100単位に丸めた値
        self.assertIn("output=567", line)
        self.assertIn("cache_read=890", line)
        # 固定プロンプトの分量なので丸めない（命中率を出すのに要る）
        self.assertIn("cache_write=432", line)

    def test_cache_hit_and_miss_are_both_visible(self):
        """命中・不命中のどちらも記録されること。

        判定プロンプトがキャッシュに当たるかで1件あたりの費用が2倍以上変わる。
        cache_read だけでは命中率が出せないので、書き込み側も要る。
        """
        hit = usage_line(
            self,
            assess_with_usage(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=6330,
                cache_creation_input_tokens=0,
            ),
        )
        miss = usage_line(
            self,
            assess_with_usage(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=6330,
            ),
        )

        self.assertIn("cache_read=6330 cache_write=0", hit)
        self.assertIn("cache_read=0 cache_write=6330", miss)

    def test_usage_goes_to_stdout(self):
        """標準エラーではなく標準出力に出す設定になっていること。"""
        logger_conf = settings.LOGGING["loggers"]["apps.judge.usage"]
        handler = settings.LOGGING["handlers"][logger_conf["handlers"][0]]

        self.assertEqual(handler["stream"], "ext://sys.stdout")
        self.assertFalse(logger_conf["propagate"])
