"""1日の判定回数の上限の回帰テスト。

ここで見るのは個人の枠（署名付き Cookie だけで数える）。上限が効くことに加えて、
日付で回復すること・改ざんで増やせないこと・壊れた Cookie で締め出さないことを
確認する。

費用の歯止めとしてのサイト全体の枠は test_spec_cost_is_capped.py、数えるために
保存するものが日付と件数だけであることは test_spec_no_stored_input.py で見る。

断るときに相談先が出ることも見るが、案内の文言そのものは
test_spec_judgment_unavailable.py が本体。ここでは「この経路からあの案内に届く」
ことだけを見る。
"""

from datetime import datetime
from unittest import mock

from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings

from .. import quota, service, views
from .helpers import JOB_TEXT, assert_consultation_is_offered


class DailyQuotaTests(TestCase):
    """1人あたり1日 4 件の上限の検証。

    サーバー側に何も持たない方針のため、カウントは署名付き Cookie だけで
    行っている。上限が効くことに加えて、日付で回復すること・判定を受け取れて
    いないときに枠を減らさないことを見る。
    """

    def _judge(self):
        return self.client.post("/", {"mode": "text", "text": JOB_TEXT})

    @override_settings(VIEW_TEST_MODE=True)
    def test_requests_up_to_the_limit_pass_and_the_next_one_is_refused(self):
        for i in range(quota.person_limit()):
            with self.subTest(nth=i + 1):
                self.assertContains(self._judge(), "危険度")

        response = self._judge()

        # 上限に達したことと、相談先が案内される
        self.assertIsNone(response.context.get("result"))
        self.assertContains(response, "本日ぶんの判定")
        self.assertContains(response, "日付が変わると")
        assert_consultation_is_offered(self, response)

    @override_settings(VIEW_TEST_MODE=True)
    def test_quota_recovers_at_the_jst_date_boundary(self):
        """日本時間の 0 時をまたぐと、また判定できること。"""
        before = datetime(2026, 8, 18, 23, 59, tzinfo=quota.JST)
        after = datetime(2026, 8, 19, 0, 1, tzinfo=quota.JST)

        with mock.patch.object(quota, "_now_jst", return_value=before):
            for _ in range(quota.person_limit()):
                self._judge()
            self.assertContains(self._judge(), "本日ぶんの判定")

        with mock.patch.object(quota, "_now_jst", return_value=after):
            self.assertContains(self._judge(), "危険度")

    @override_settings(VIEW_TEST_MODE=False)
    def test_unavailable_judgment_does_not_consume_the_quota(self):
        """API 側の事情で判定を受け取れなかった分は、枠を減らさないこと。"""
        with mock.patch.object(
            views,
            "job_offer_risk_assess",
            return_value=service.AssessmentError.UNAVAILABLE,
        ):
            for _ in range(quota.person_limit() + 1):
                self.assertContains(self._judge(), "いまは、AIによる判定を行えません")

        # 一度も判定を受け取っていないので、枠は満額残っている
        with override_settings(VIEW_TEST_MODE=True):
            self.assertContains(self._judge(), "危険度")


@override_settings(VIEW_TEST_MODE=True)
class QuotaCookieIsVerifiedTests(TestCase):
    """回数カウントの Cookie を書き換えたときの振る舞いの検証。

    カウントはブラウザ側の署名付き Cookie だけで持っているため、署名を
    検証していなければ上限そのものが意味を失う。一方で、壊れた Cookie で
    利用者を締め出してもいけない（quota.py の方針どおり「枠が戻る」側に
    倒す）。向きが逆の2つなので、どちらも固定しておく。

    Cookie を消す・別のブラウザを使う経路は回避できる（README に明記の
    とおり）。費用の歯止めは全体の枠側にあり、そちらは SiteDailyLimitTests
    で見ている。
    """

    NOW = datetime(2026, 9, 22, 12, 0, tzinfo=quota.JST)
    TODAY = "2026-09-22"

    def _sign(self, raw):
        """アプリと同じ経路で Cookie の値に署名する。

        署名に使う salt の組み方は Django の内部仕様（6.1 で変わった）なので、
        自分で組み立てず、公開 API に書かせた結果を借りる。
        """
        carrier = HttpResponse()
        carrier.set_signed_cookie(quota.COOKIE_NAME, raw, salt=quota.COOKIE_SALT)
        return carrier.cookies[quota.COOKIE_NAME].value

    def _unsign(self, value):
        """アプリと同じ経路で署名を検証し、中身を取り出す。"""
        request = RequestFactory().post("/")
        request.COOKIES[quota.COOKIE_NAME] = value
        return request.get_signed_cookie(quota.COOKIE_NAME, salt=quota.COOKIE_SALT)

    def _judge(self, client=None):
        with mock.patch.object(quota, "_now_jst", return_value=self.NOW):
            return (client or self.client).post("/", {"text": JOB_TEXT})

    def test_a_correctly_signed_count_is_honoured(self):
        """署名が合っていれば、その件数から数え始めること。"""
        self.client.cookies[quota.COOKIE_NAME] = self._sign(
            f"{self.TODAY}:{quota.person_limit() - 1}"
        )

        self.assertContains(self._judge(), "危険度")  # 残り1件
        self.assertContains(self._judge(), "本日ぶんの判定")  # 使い切り

    def test_a_tampered_count_is_not_honoured(self):
        """署名を書き換えた件数は読まないこと。

        読んでしまうと、件数を小さく書き換えるだけで上限が外れる。
        検知したときは 0 として扱うので、枠は満額に戻る（締め出さない方針）。
        """
        signed = self._sign(f"{self.TODAY}:{quota.person_limit() - 1}")
        self.client.cookies[quota.COOKIE_NAME] = signed[:-1] + (
            "a" if signed[-1] != "a" else "b"
        )

        for i in range(quota.person_limit()):
            with self.subTest(nth=i + 1):
                self.assertContains(self._judge(), "危険度")
        self.assertContains(self._judge(), "本日ぶんの判定")

    def test_a_broken_cookie_never_locks_the_user_out(self):
        """読めない Cookie でも 500 にせず、判定を通すこと。"""
        broken = {
            "空": "",
            "署名のない平文": f"{self.TODAY}:0",
            "でたらめな値": "garbage",
            "件数が数値でない": self._sign(f"{self.TODAY}:abc"),
            "件数が負": self._sign(f"{self.TODAY}:-5"),
            "昨日の日付": self._sign(f"2026-09-21:{quota.person_limit()}"),
        }
        for label, value in broken.items():
            with self.subTest(cookie=label):
                client = Client()
                client.cookies[quota.COOKIE_NAME] = value

                self.assertContains(self._judge(client), "危険度")

    def test_the_count_written_back_is_capped_at_the_limit(self):
        """書き戻す件数は上限で止めること。"""
        request = RequestFactory().post("/")
        request.COOKIES[quota.COOKIE_NAME] = self._sign(f"{self.TODAY}:99")
        response = HttpResponse()

        with mock.patch.object(quota, "_now_jst", return_value=self.NOW):
            quota.consume(request, response)

        written = self._unsign(response.cookies[quota.COOKIE_NAME].value)
        self.assertEqual(written, f"{self.TODAY}:{quota.person_limit()}")

    def test_the_cookie_expires_at_the_next_jst_midnight(self):
        """Cookie の寿命が翌0時までであること（日付をまたいで残らない）。"""
        cases = [
            ("23:59", datetime(2026, 9, 22, 23, 59, tzinfo=quota.JST), 60),
            ("00:01", datetime(2026, 9, 22, 0, 1, tzinfo=quota.JST), 86340),
        ]
        for label, now, expected in cases:
            with self.subTest(now=label):
                client = Client()
                with mock.patch.object(quota, "_now_jst", return_value=now):
                    response = client.post("/", {"text": JOB_TEXT})

                self.assertEqual(
                    int(response.cookies[quota.COOKIE_NAME]["max-age"]), expected
                )

    def test_the_cookie_is_marked_secure_outside_debug(self):
        """DEBUG=False（本番）では secure が付くこと。

        ローカルは DEBUG=True で動かすため開発中は付かない。環境差で
        見え方が変わらないよう、どちらの値もテスト側で固定する。
        """
        for debug, secure in [(False, True), (True, False)]:
            with self.subTest(DEBUG=debug):
                client = Client()
                with override_settings(DEBUG=debug):
                    response = self._judge(client)

                cookie = response.cookies[quota.COOKIE_NAME]
                self.assertEqual(bool(cookie["secure"]), secure)
