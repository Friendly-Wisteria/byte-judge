"""差し替えたエラーページに、相談先が出ることの検証（#50）。

判定を返せないときに相談先を添える約束は、views 側の案内（_unavailable）と
500 ページで守っている。ただし Django が view より手前で弾く経路（CSRF 検証の
失敗・リクエストの解釈失敗・URL の取り違え）は、そのどちらにも載らず、既定の
英語のページが返っていた。

ここでは差し替えた画面そのものと、その画面に実際に届く経路の両方を見る。
判定の経路に載らない例外で出る 500 ページも、守る約束は同じなのでここで見る。
"""

from django.template import loader
from django.test import Client, RequestFactory, SimpleTestCase, override_settings

from .. import consultation, views
from .helpers import FORBIDDEN_ON_THE_ERROR_PAGE

# 差し替えたページ。素の英語ページに戻っていないことと、相談先が載っている
# ことを、同じ条件（context 無し）で確かめる。
REPLACED_ERROR_TEMPLATES = ("400.html", "403_csrf.html")


class ReplacedErrorPagesOfferConsultationTests(SimpleTestCase):
    """差し替えた 400 / 403 のページに、相談先が載っていることの検証。

    描画はテストクライアントを通さず、Django の既定 view と同じ条件
    （context 無し）で行う。経路の都合（DEBUG・ALLOWED_HOSTS）が混ざると、
    何を見ているのかがぼやけるため。経路そのものは下のクラスで見る。
    """

    def _render(self, template_name):
        return loader.get_template(template_name).render()

    def test_every_contact_reaches_the_pages(self):
        """相談先の番号と用途が、どちらのページにも出ること。
        変異テスト: テンプレートから {% consultation_guide %} を外す
        """
        for template_name in REPLACED_ERROR_TEMPLATES:
            html = self._render(template_name)
            for name, when in consultation.CONSULTATION_CONTACTS:
                with self.subTest(template=template_name, contact=name):
                    self.assertIn(name, html)
                    self.assertIn(when, html)

    def test_the_heading_reaches_the_pages(self):
        """相談を促す一文も、番号と一緒に出ること。

        番号を並べるだけでは、自分が相談してよい立場なのか判断がつかない。
        変異テスト: 見出しの文言をテンプレートに直書きする
        """
        for template_name in REPLACED_ERROR_TEMPLATES:
            with self.subTest(template=template_name):
                self.assertIn(
                    consultation.CONSULTATION_HEADING, self._render(template_name)
                )

    def test_no_technical_detail_leaks_onto_the_pages(self):
        """内部情報を出さないこと。

        400 はホスト名の設定ミスでも出るため、設定値や例外クラス名が載ると
        構成を外に教えることになる。
        """
        for template_name in REPLACED_ERROR_TEMPLATES:
            html = self._render(template_name)
            for token in FORBIDDEN_ON_THE_ERROR_PAGE:
                with self.subTest(template=template_name, token=token):
                    self.assertNotIn(token, html)

    def test_the_400_page_does_not_blame_the_visitor(self):
        """400 は原因を断定しないこと。

        この画面には、ホスト名の設定ミス（利用者に非がない）と、極端に長い
        テキストの送信（送った内容が原因）の両方が来る。500 と同じ
        「あなたの操作や入力が原因ではありません」を流用すると、後者に対して
        事実と違う案内になる。
        変異テスト: 500.html の文面をそのまま持ち込む
        """
        self.assertNotIn("あなたの操作や入力が原因ではありません", self._render("400.html"))


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class TheErrorRoutesReachTheReplacedPagesTests(SimpleTestCase):
    """差し替えたページに、実際の経路から届くことの検証。

    DEBUG=True では Django が開発用の画面を出すため、本番と同じ DEBUG=False で
    確かめる。
    """

    def test_a_csrf_failure_offers_consultation(self):
        """Cookie が無い状態で送っても、相談先が出ること。

        Cookie を受け付けない環境（プライベートブラウジング・Cookie の全面
        ブロック・共用端末での削除）からは、判定を一度も送れない。素の英語
        ページで締め出さない。
        変異テスト: 403_csrf.html を消す
        """
        response = Client(enforce_csrf_checks=True).post(
            "/", {"mode": "text", "text": "日給5万円 即日手渡し"}
        )

        self.assertEqual(response.status_code, 403)
        for name, _ in consultation.CONSULTATION_CONTACTS:
            with self.subTest(contact=name):
                self.assertContains(response, name, status_code=403)

    def test_a_disallowed_host_offers_consultation(self):
        """ALLOWED_HOSTS 外のホスト名で来ても、相談先が出ること。
        変異テスト: 400.html を消す / handler400 を外す
        """
        with self.assertLogs("apps.judge.views", level="ERROR"):
            response = self.client.get("/", headers={"host": "other.example.com"})

        self.assertEqual(response.status_code, 400)
        for name, _ in consultation.CONSULTATION_CONTACTS:
            with self.subTest(contact=name):
                self.assertContains(response, name, status_code=400)

    def test_the_hostnames_stay_out_of_the_page(self):
        """届いたホスト名も、設定値も、画面には出さないこと。

        値が必要なのは設定を直す人で、その人はログを読める。画面に出すと、
        構成を外に教えることになる。
        変異テスト: 400.html に exception や ALLOWED_HOSTS を埋め込む
        """
        with self.assertLogs("apps.judge.views", level="ERROR"):
            response = self.client.get("/", headers={"host": "other.example.com"})

        self.assertNotContains(response, "other.example.com", status_code=400)
        self.assertNotContains(response, "testserver", status_code=400)

    def test_the_configured_hosts_reach_the_log(self):
        """設定されている ALLOWED_HOSTS が、ログに出ること。

        Django が出すのは届いた Host の値だけで、設定側に何が入っているかは
        出ない。突き合わせができないと、原因の切り分けに時間がかかる。
        変異テスト: views.bad_request のログを消す / handler400 を外す
        """
        with self.assertLogs("apps.judge.views", level="ERROR") as logs:
            self.client.get("/", headers={"host": "other.example.com"})

        self.assertIn("testserver", "\n".join(logs.output))

    def test_other_400s_do_not_report_the_hosts(self):
        """ホスト名の設定と関係ない 400 では、ALLOWED_HOSTS を出さないこと。

        RequestDataTooBig なども同じ handler を通る。そこで設定値を出しても
        手がかりにならず、ログのノイズになる。
        変異テスト: bad_request から DisallowedHost の判定を外す
        """
        with self.assertNoLogs("apps.judge.views", level="ERROR"):
            views.bad_request(
                RequestFactory().get("/"), ValueError("not a host problem")
            )

    def test_an_unknown_url_leads_back_to_the_form(self):
        """存在しない URL は、エラーページを見せずに入力画面へ送ること。

        ここに来る人がやることは結局「最初から入力する」なので、英語の
        エラーページを挟まない。
        変異テスト: handler404 を外す
        """
        response = self.client.get("/no-such-page/")

        self.assertRedirects(response, "/", status_code=302)


class ServerErrorPageOffersConsultationTests(SimpleTestCase):
    """素の 500 ページの差し替えの検証。

    判定の経路に載らない想定外の例外では Django の既定の 500 ページが返る。
    そこにも相談先が出ること、そして相談先が consultation.py から来ている
    こと（片方だけ古くならないこと）を見る。

    server_error は context も request も渡さずに render() するため、
    テストも同じ条件で呼ぶ。テストクライアント経由にすると DEBUG や
    ALLOWED_HOSTS の影響が混ざり、何を見ているのかがぼやける。
    """

    def _render(self):
        """server_error と同じ条件で 500.html を描画する。"""
        return loader.get_template("500.html").render()

    def test_every_contact_reaches_the_page(self):
        """500ページに、相談先の情報が掲載されていること。

        実装のミスなどで、正常に動作していない時に、
        公的な窓口と、どんな場合に利用するかを案内することを保証する。
        """
        html = self._render()
        for name, when in consultation.CONSULTATION_CONTACTS:
            with self.subTest(contact=name):
                self.assertIn(name, html)
                self.assertIn(when, html)

    def test_the_heading_reaches_the_page(self):
        """相談を促す一文も、番号と一緒に出ること。

        番号を並べるだけでは、自分が相談してよい立場なのか判断がつかない。
        声をかける一文が落ちると、案内の性格そのものが変わる。
        """
        self.assertIn(consultation.CONSULTATION_HEADING, self._render())

    def test_every_contact_reaches_the_message_text(self):
        """メッセージ側にも、同じ相談先が載っていること。

        サービスが正常に動作しない状況で、公的な相談窓口の紹介が
        一貫していることを保証する。
        """
        for name, _ in consultation.CONSULTATION_CONTACTS:
            with self.subTest(contact=name):
                self.assertIn(name, views.CONSULTATION_GUIDE)

    def test_no_technical_detail_leaks_onto_the_page(self):
        """500 ページにも、内部情報を出さないこと。

        この画面は DB 障害で出ることが多い。接続先や例外クラス名が載っても
        利用者には手がかりにならず、構成を外に教えることになるだけ。
        """
        html = self._render()
        for token in FORBIDDEN_ON_THE_ERROR_PAGE:
            with self.subTest(token=token):
                self.assertNotIn(token, html)
