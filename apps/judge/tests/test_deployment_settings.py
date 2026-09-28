"""デプロイ時の前提の回帰テスト。

設定値を目で確かめるだけでは、将来の変更で外れても気づけない。実際に
リクエストを通したり Django のチェックを走らせたりして、本番で効いていて
ほしい前提が生きていることを確かめる。

adminについては、現時点では削除し、後でAPI利用状況の追跡のために実装予定
"""

import importlib.util
import os
import unittest
from unittest import mock

import environ
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .helpers import JOB_TEXT

SETTINGS_MODULE = "config.settings"


def _load_fresh_settings(env_overrides, remove=()):
    """環境変数を差し替えた状態で、settingsを別モジュールとして新規ロードする。

    `@override_settings()`デコレーターでは、settings.pyに読み込んだ後の値を書き換えるので、
    .envごとテスト用に用意し直した上で、それを前提にsettings.pyを走らせる必要がある。
    """
    origin = importlib.util.find_spec(SETTINGS_MODULE).origin
    spec = importlib.util.spec_from_file_location("_settings_under_test", origin)
    module = importlib.util.module_from_spec(spec)

    with mock.patch.dict(os.environ, env_overrides):
        for key in remove:
            os.environ.pop(key, None)
        with mock.patch.object(environ.Env, "read_env"):
            spec.loader.exec_module(module)
    return module


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


class HttpsRedirectIsWiredForTheProxyTests(SimpleTestCase):
    """HTTPS へのリダイレクトが、前段終端の構成で正しく働くことの検証。

    デプロイ先（Cloud Run）は TLS をフロントエンドで終端し、コンテナへは平文の
    HTTP で渡す。Django から見たリクエストは常に http になるため、
    SECURE_PROXY_SSL_HEADER でヘッダを信じる設定が無いまま
    SECURE_SSL_REDIRECT を有効にすると、何度飛ばしても http のままで
    リダイレクトループになる（#31）。

    settings.py 側はこの2つを DEBUG=False のときだけ組にして定義しているが、
    CI では SECURE_SSL_REDIRECT を環境変数で False にして走らせている
    （テストクライアントは平文で来るため）。ここでは本番と同じ値を明示的に
    与えて、組み合わせの挙動だけを見る。
    """

    PRODUCTION_HTTPS_SETTINGS = {
        "DEBUG": False,
        "ALLOWED_HOSTS": ["example.com"],
        "SECURE_SSL_REDIRECT": True,
        "SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https"),
    }

    def test_a_plain_request_is_sent_to_https(self):
        with self.settings(**self.PRODUCTION_HTTPS_SETTINGS):
            response = self.client.get("/", headers={"host": "example.com"})

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://example.com/")

    def test_a_request_forwarded_as_https_is_served_as_is(self):
        """Djangoの仕様で、前段が https で終端した印（X-Forwarded-Proto）があれば、飛ばさない。

        ここが 301 になる構成はリダイレクトループそのもので、公開後は全ページが
        開けなくなる。
        """
        with self.settings(**self.PRODUCTION_HTTPS_SETTINGS):
            response = self.client.get(
                "/",
                headers={"host": "example.com", "x-forwarded-proto": "https"},
            )

        self.assertEqual(response.status_code, 200)

    def test_secure_ssl_redirect_is_undefined_in_dev_env(self):
        """開発環境で、SECURE_SSL_REDIRECTが無効であること。

        開発環境では必要のない設定なので、未設定であることを固定
        """
        s = _load_fresh_settings(
            {
                "DEBUG": "True",
                "SECRET_KEY": "test",
                "DATABASE_URL": "sqlite:///db.sqlite3",
            },
            remove=("SECURE_SSL_REDIRECT",),
        )
        self.assertFalse(hasattr(s, "SECURE_SSL_REDIRECT"))

    def test_secure_proxy_ssl_header_is_undefined_in_dev_env(self):
        """開発環境で、SECURE_PROXY_SSL_HEADERが設定されていないこと。

        開発環境では必要のない設定なので、未設定であることを固定
        """
        s = _load_fresh_settings(
            {
                "DEBUG": "True",
                "SECRET_KEY": "test",
                "DATABASE_URL": "sqlite:///db.sqlite3",
            }
        )
        self.assertFalse(hasattr(s, "SECURE_PROXY_SSL_HEADER"))

    def test_secure_ssl_redirect_is_active_in_prod_env(self):
        """本番環境でSECURE_SSL_REDIRECT=Trueになっていること。

        ここが有効になっていないと、上のテスト2件で固定したDjangoの仕様を使っていないことになる。
        実行時の環境変数にSECURE_SSL_REDIRECT=Falseが入っているため、既定値を見るにはremoveが必要
        """
        s = _load_fresh_settings(
            {
                "DEBUG": "False",
                "SECRET_KEY": "test",
                "DATABASE_URL": "sqlite:///db.sqlite3",
            },
            remove=("SECURE_SSL_REDIRECT",),
        )
        self.assertTrue(s.SECURE_SSL_REDIRECT)

    def test_secure_proxy_ssl_header_is_set_correctly_in_prod_env(self):
        """本番環境で前段が https で終端した印（X-Forwarded-Proto）があれば、飛ばさない設定になっていること。

        ここが有効になっていないと、上のテスト2件で固定したDjangoの仕様を使っていないことになる。
        """
        s = _load_fresh_settings(
            {
                "DEBUG": "False",
                "SECRET_KEY": "test",
                "DATABASE_URL": "sqlite:///db.sqlite3",
            }
        )
        self.assertEqual(s.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))


class DatabaseURLSettingsTests(SimpleTestCase):
    """本番環境では、DATABASE_URL の明示的な設定が必須であることの検証。

    永続ディスクのない環境では、フォールバック先の SQLite にも例外を出さずに
    書けてしまう。その状態で動くと DailyUsage がインスタンスごと・再起動ごとに
    分かれ、SITE_DAILY_LIMIT の歯止めが静かに外れるため、起動時に止める。
    """

    def test_allow_no_database_url_for_debug_environment(self):
        """開発環境であれば、DATABASE_URLが未設定の場合デフォルトにフォールバックする。"""
        s = _load_fresh_settings(
            {"DEBUG": "True", "SECRET_KEY": "test"},
            remove=("DATABASE_URL",),
        )
        db = s.DATABASES["default"]
        self.assertEqual(db["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(db["NAME"], str(s.BASE_DIR / "db.sqlite3"))

    def test_denies_no_database_url_for_prod_environment(self):
        """本番環境であれば、DATABASE_URLが未設定の場合ImproperlyConfiguredを発する。"""
        with self.assertRaisesRegex(ImproperlyConfigured, "DATABASE_URL"):
            _load_fresh_settings(
                {"DEBUG": "False", "SECRET_KEY": "test"},
                remove=("DATABASE_URL",),
            )

    def test_production_accepts_database_url(self):
        """本番環境では、SQLite3もPostgreSQLも通す。"""
        cases = [
            (
                "postgresql",
                "postgres://user:pass@localhost:5432/app",
                "django.db.backends.postgresql",
                "app",
            ),
            (
                "sqlite3",
                "sqlite:////data/prod.sqlite3",
                "django.db.backends.sqlite3",
                "/data/prod.sqlite3",
            ),
        ]
        for label, url, engine, name in cases:
            with self.subTest(label):
                s = _load_fresh_settings(
                    {"DEBUG": "False", "SECRET_KEY": "test", "DATABASE_URL": url},
                )
                db = s.DATABASES["default"]
                self.assertEqual(db["ENGINE"], engine)
                self.assertEqual(db["NAME"], name)


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

    いま残っているのは HSTS をどこまで広げるかの2件だけで、どちらも「まだ
    出さない」ことが判断の結果として残っているもの（#31）。リダイレクトの
    挙動は HttpsRedirectIsWiredForTheProxyTests、判定回数の Cookie の Secure は
    quota.py 側で、それぞれ別に見ている。

    settings.py の SECURE_* は `if not DEBUG` の中にあるため、この検証は
    DEBUG=False で読み込まれたときにしか意味を持たない。CI がその条件で走るので、
    手元（DEBUG=True）では skip する。

    ここで固定しておくと、新しい指摘が増えたときに気づける。残りを解消したら、
    このリストからも消すこと。
    """

    KNOWN_HTTPS_WARNINGS = {
        # サブドメインまで HTTPS を強制する宣言。カスタムドメインの構成が
        # 決まってから判断する。
        "security.W005",  # SECURE_HSTS_INCLUDE_SUBDOMAINS
        # ブラウザへの事前登録。取り消しが効きにくいので、SECURE_HSTS_SECONDS を
        # 十分に伸ばして運用が安定してから。
        "security.W021",  # SECURE_HSTS_PRELOAD
    }

    # SECRET_KEY は環境ごとに違う（CI はダミー値、手元は .env の値）。鍵が短いと
    # security.W009 が増えるため、ここでは十分な長さの固定値に差し替えて、
    # settings.py に書いてある設定だけを見る。本番の鍵についての約束は
    # README「本番環境にデプロイする場合の必須設定」側に置いている。
    STRONG_ENOUGH_SECRET_KEY = (
        "test-only-key-" + "Xq7mZ2vB9nL4wP6sT1cR8hJ5dK3gF0yAeU2iO5pW"
    )

    # SECURE_SSL_REDIRECT は CI だけ環境変数で False にしているため、settings.py
    # の値をそのまま見ると実行環境によって結果が変わる。本番の値を明示して、
    # どこで走らせても同じ指摘になるようにする。
    # Django のテストランナーは実行中 settings.DEBUG を強制的に False にするため、
    # settings.DEBUG では「本番の設定が読み込まれているか」を判定できない。
    # settings.py が見たのと同じ環境変数を、同じ読み方で確かめる。
    @unittest.skipUnless(
        not environ.Env().bool("DEBUG", default=False),
        "settings.py の SECURE_* は DEBUG=False のときだけ定義される（CI で検証する）",
    )
    @override_settings(
        DEBUG=False,
        ALLOWED_HOSTS=["example.com"],
        SECRET_KEY=STRONG_ENOUGH_SECRET_KEY,
        SECURE_SSL_REDIRECT=True,
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

    def test_admin_urls_do_not_reach_an_admin_page(self):
        """
        adminのURLが、どこにも繋がっていないこと
        ## 期待する挙動
        `/admin`と`/admin/login`が、トップへのリダイレクトになる

        404 はトップへのリダイレクトに差し替えたため（#50）、status は 302 に
        なる。確かめたいのは「adminの画面が出ないこと」なので、リダイレクト先
        まで追って見る。
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
        adminを足すと`/admin/`が200でログイン画面を返すため、リダイレクトの
        確認で落ちる。
        # 必要性
        未実装のadminが総当たり攻撃の的となりえないように封鎖していることを固定する。
        """
        for path in ("/admin", "/admin/", "/admin/login", "/admin/login/"):
            with self.subTest(path=path):
                response = self.client.get(path, follow=True)
                self.assertEqual(response.redirect_chain[-1], ("/", 302))
                self.assertNotContains(response, "Django administration")
