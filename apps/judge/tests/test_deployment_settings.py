"""デプロイ時の前提の回帰テスト。

設定値を目で確かめるだけでは、将来の変更で外れても気づけない。実際に
リクエストを通したり Django のチェックを走らせたりして、本番で効いていて
ほしい前提が生きていることを確かめる。

adminについては、現時点では削除し、後でAPI利用状況の追跡のために実装予定
"""

from django.conf import settings
from django.core import checks
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .helpers import JOB_TEXT


@override_settings(VIEW_TEST_MODE=True)
class CsrfProtectionIsEnforcedTests(TestCase):
    """判定フォームの POST が CSRF で守られていることの検証。

    既定のテストクライアントは CSRF チェックを飛ばすため、保護が外れても
    他のテストはすべて緑のままになる。ここだけ enforce_csrf_checks=True で
    実際の経路を通す。
    """

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def test_a_post_without_a_token_is_refused(self):
        response = self.client.post("/", {"mode": "text", "text": JOB_TEXT})

        self.assertEqual(response.status_code, 403)

    def test_a_post_from_the_page_still_goes_through(self):
        """守りが効いていても、画面から普通に送れば通ること。"""
        page = self.client.get("/")

        response = self.client.post(
            "/",
            {
                "mode": "text",
                "text": JOB_TEXT,
                "csrfmiddlewaretoken": page.context["csrf_token"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("result"))

    def test_the_form_carries_a_token(self):
        """テンプレート側の {% csrf_token %} が残っていること。"""
        self.assertContains(self.client.get("/"), "csrfmiddlewaretoken")


class AllowedHostsMustBeConfiguredTests(SimpleTestCase):
    """ALLOWED_HOSTS が空のまま本番に出たときの挙動の検証。

    runserver は起動時に止まるが、gunicorn などではこのチェックが走らない。
    設定漏れに気づくのがデプロイ後の最初のリクエストになるため、そのときに
    何が起きるか（全部拒否される）を固定しておく。
    """

    @override_settings(ALLOWED_HOSTS=[], DEBUG=False)
    def test_every_request_is_refused_when_the_list_is_empty(self):
        response = self.client.get("/", headers={"host": "example.com"})

        self.assertEqual(response.status_code, 400)

    @override_settings(ALLOWED_HOSTS=["example.com"], DEBUG=False)
    def test_only_the_listed_host_is_served(self):
        self.assertEqual(
            self.client.get("/", headers={"host": "evil.example"}).status_code, 400
        )
        self.assertEqual(
            self.client.get("/", headers={"host": "example.com"}).status_code, 200
        )


class MigrationsMatchTheModelsTests(TestCase):
    """モデルとマイグレーションがずれていないことの検証。

    ずれたままデプロイすると、本番の最初の書き込みで初めて分かる。
    このテストは CI でも走るので、PR の時点で気づける。
    """

    def test_no_migration_is_missing(self):
        try:
            call_command("makemigrations", "--check", "--dry-run", verbosity=0)
        except SystemExit:
            self.fail(
                "モデルの変更に対してマイグレーションが作られていません。"
                "`python manage.py makemigrations` を実行してください。"
            )


class DeploymentWarningsAreAccountedForTests(SimpleTestCase):
    """`manage.py check --deploy` の指摘が、把握済みのものだけであることの検証。

    いま残っているのは HTTPS 関係の4件。置き場所（リバースプロキシの有無）が
    決まらないと判断できないため、あえて未設定にしている（#31）。判定回数の
    Cookie だけは quota.py 側で Secure を立てている。

    ここで固定しておくと、新しい指摘が増えたときに気づける。4件を解消したら、
    このリストからも消すこと。
    """

    # ALLOWED_HOSTS を設定した状態でも残るもの（W020 は設定すれば消える）
    KNOWN_HTTPS_WARNINGS = {
        "security.W004",  # SECURE_HSTS_SECONDS
        "security.W008",  # SECURE_SSL_REDIRECT
        "security.W012",  # SESSION_COOKIE_SECURE
        "security.W016",  # CSRF_COOKIE_SECURE
    }

    # SECRET_KEY は環境ごとに違う（CI はダミー値、手元は .env の値）。鍵が短いと
    # security.W009 が増えるため、ここでは十分な長さの固定値に差し替えて、
    # settings.py に書いてある設定だけを見る。本番の鍵についての約束は
    # README「本番環境にデプロイする場合の必須設定」側に置いている。
    STRONG_ENOUGH_SECRET_KEY = (
        "test-only-key-" + "Xq7mZ2vB9nL4wP6sT1cR8hJ5dK3gF0yAeU2iO5pW"
    )

    @override_settings(
        DEBUG=False,
        ALLOWED_HOSTS=["example.com"],
        SECRET_KEY=STRONG_ENOUGH_SECRET_KEY,
    )
    def test_no_unexpected_warning_appears(self):
        found = {
            message.id for message in checks.run_checks(include_deployment_checks=True)
        }

        self.assertEqual(found, self.KNOWN_HTTPS_WARNINGS)


class AdminDeploymentTests(SimpleTestCase):
    def test_django_contrib_admin_not_installed(self):
        """
        django.contrib.adminがインストールされていないこと
        # Mutation Test
        `settings.py`
        ```python
        INSTALLED_APPS = [
            "django.contrib.admin", #<- この行を追加
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "apps.judge",
        ]
        ```
        # 必要性
        現時点では、adminを実装していない。
        そのため、総当たり攻撃の的となり得るadminのログイン画面を封鎖していることを固定する
        """

        self.assertFalse("django.contrib.admin" in settings.INSTALLED_APPS)

    def test_admin_url_returns_404(self):
        """
        adminのURLが存在しないこと
        ## 期待する挙動
        `/admin`と`/admin/login`が404を返す
        # Mutation Test
        `config.urls.py`に、以下の2行を追加
        ```python
        from django.contrib import admin #<-この行を追加
        from django.urls import include, path

        urlpatterns = [
            path("admin/", admin.site.urls), #<-この行を追加
            path("", include("apps.judge.urls")),
        ]
        ```
        # 必要性
        未実装のadminが総当たり攻撃の的となりえないように封鎖していることを固定する。
        """
        self.assertEqual(self.client.get("/admin").status_code, 404)
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertEqual(self.client.get("/admin/login").status_code, 404)
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)
