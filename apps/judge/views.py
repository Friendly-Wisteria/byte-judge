import logging

from django.conf import settings
from django.contrib import messages
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.generic import FormView, TemplateView

from . import quota
from .forms import JobOfferRiskAssessForm
from .service import AssessmentError, job_offer_risk_assess

logger = logging.getLogger(__name__)

# 判定を返せないときに必ず添える相談先。判定が止まっていても、相談先の情報だけは
# 届ける必要がある（危険な求人を前にした人を、案内なしで締め出さない）。
# 上限・API 障害・拒否・パース失敗のどの経路でも、これを末尾に付ける。
CONSULTATION_GUIDE = (
    "不安なときは、ひとりで抱えずに相談してください。\n"
    "・警察相談専用ダイヤル #9110（犯罪かもしれない、と思ったとき）\n"
    "・消費者ホットライン 188（いやや）（お金を払ってしまったとき）"
)

# 判定不可の案内であることをテンプレートに伝える印。入力の直し方を案内する
# バリデーションエラーとは見え方を変えるために使う。
UNAVAILABLE_TAG = "judgment-unavailable"

# 配線テストモードで出す警告。判定は LLM に投げず fixtures から1件を選ぶため、
# 見た目は本物と区別がつかない。この一文だけが誤認を防いでいる。
VIEW_TEST_MODE_WARNING = (
    "現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。"
)


def _unavailable(reason: str) -> str:
    """判定不可の案内を組み立てる。相談先が必ず末尾に付く形にする。

    文言をここに通すことで、経路ごとに相談先を書き忘れることがなくなる。
    改行はテンプレート側で <br> にして、「理由 / これからどうなるか /
    相談先」の3段に見せる。
    """
    return f"{reason}\n\n{CONSULTATION_GUIDE}"


# LLM 側の事情（月額の利用上限・レート制限・API 障害・安全機構による拒否）で
# 判定を受けられないときの案内。利用者が入力を直しても解消しないため、
# 「あなたの書き方の問題ではない」ことが分かる書き方にする。
LLM_UNAVAILABLE_ERROR = _unavailable(
    "いまは、AIによる判定を行えません。\n"
    "サービス側の問題なので、文章を直しても解決しません。"
    "時間をおくと使えるようになることがありますが、いつ戻るかはお約束できません。"
)

# 応答は得られたが、判定結果として受け取れなかった場合（構造化出力のパース
# 失敗・出力の打ち切りなど）。再試行で通ることがあるため、そちらを先に案内する。
ASSESSMENT_FAILED_ERROR = _unavailable(
    "判定の結果を、正しく受け取れませんでした。\n"
    "もう一度お試しください。何度試しても同じときは、時間をおいてからお試しください。"
)

# サイト全体の枠を使い切ったときの案内。個人の上限と混同されないよう、
# 「自分の使いすぎではない」ことが分かる書き方にする。
SITE_QUOTA_ERROR = _unavailable(
    "本日ぶんの判定枠（サイト全体）が埋まりました。あなたの使いすぎではありません。\n"
    "日付が変わると（日本時間の0時）、また使えるようになります。"
)


# 1日の上限に達したときの案内。件数は設定から取るため、読み込み時ではなく
# 呼ばれた時に組み立てる。
def daily_quota_error() -> str:
    """個人の上限に達したときの案内。"""
    return _unavailable(
        f"本日ぶんの判定（1日{quota.person_limit()}件）を使い切りました。\n"
        "日付が変わると（日本時間の0時）、また使えるようになります。"
    )


def _report_unavailable(request, text: str) -> None:
    """判定不可の案内を、見出し付きで表示させる印とともに積む。"""
    messages.error(request, text, extra_tags=UNAVAILABLE_TAG)


# エラー報告（DEBUG=True 時の 500 ページ、ADMINS 設定時の管理者メール）に
# POST の求人テキストが載らないようにする。
# あわせて、判定結果のページがブラウザやプロキシのキャッシュに保存されないよう
# no-store を返す。履歴と「戻る」操作までは防げない点に注意。
@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class IndexView(FormView):
    template_name = "judge/index.html"
    form_class = JobOfferRiskAssessForm

    def get(self, request, *args, **kwargs):
        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(
                request,
                VIEW_TEST_MODE_WARNING,
            )
        return super().get(request, *args, **kwargs)

    # 例外レポートのローカル変数一覧に求人テキストが載らないようにする
    @sensitive_variables()
    def form_valid(self, form):
        # 上限に達している場合は、API を叩かずに案内だけ返す
        if quota.is_exhausted(self.request):
            # 入力内容は残さないため、件数以外は出さない
            logger.info("Daily quota reached")
            _report_unavailable(self.request, daily_quota_error())
            return self.render_to_response(self.get_context_data(form=form))

        # 全体の枠を確保する。取れなければ API は叩かない。個人の枠と違い、
        # 判定を返せたかどうかではなく「API に投げるか」で数える
        # （拒否や打ち切りでも、出力ぶんの費用は出ているため）。
        if not settings.VIEW_TEST_MODE:
            is_reserved, _ = quota.reserve_site_slot()
            if not is_reserved:
                _report_unavailable(self.request, SITE_QUOTA_ERROR)
                return self.render_to_response(self.get_context_data(form=form))

        # 画像入力は停止中。フォームに image フィールドが無いため、POST に
        # image を含めても cleaned_data には載らず、判定はテキストだけで行う。
        result = job_offer_risk_assess(form.cleaned_data["text"])

        # 判定できなかった場合、サービス層は結果の代わりに理由を返す。
        # どちらも結果は表示せず、理由に応じた案内を出す。
        if result is None or isinstance(result, AssessmentError):
            if result is AssessmentError.UNAVAILABLE:
                # LLM に判定させること自体ができていない状態。
                logger.error("Risk assessment unavailable: no judgment from the LLM")
                _report_unavailable(self.request, LLM_UNAVAILABLE_ERROR)
            else:
                logger.error("Risk assessment failed: service returned %r", result)
                _report_unavailable(self.request, ASSESSMENT_FAILED_ERROR)
            return self.render_to_response(self.get_context_data(form=form))

        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(
                self.request,
                VIEW_TEST_MODE_WARNING,
            )

        response = self.render_to_response(
            self.get_context_data(form=form, result=result)
        )
        # 判定を返せたときだけ 1 件として数える。API 障害・利用上限で判定を
        # 受け取れなかった場合（AssessmentError）は、ここに来ないので消費しない。
        quota.consume(self.request, response)
        return response

    def form_invalid(self, form):
        # サーバ側バリデーションのエラーを messages に載せてテンプレートで表示
        for errors in form.errors.values():
            for error in errors:
                messages.error(self.request, error)
        return super().form_invalid(form)


class PrivacyPolicyView(TemplateView):
    template_name = "judge/privacy.html"
