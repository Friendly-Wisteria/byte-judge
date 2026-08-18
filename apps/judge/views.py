import logging

from django.conf import settings
from django.contrib import messages
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.generic import FormView
from PIL import Image

from . import quota
from .forms import JobOfferRiskAssessForm
from .service import AssessmentError, job_offer_risk_assess

logger = logging.getLogger(__name__)

# LLM 側の事情（月額の利用上限・レート制限・API 障害・安全機構による拒否）で
# 判定を受けられないときの案内。時間をおいても回復するとは限らないため、
# 再試行の案内だけで終わらせず、相談先（ページ下部の注意書き）まで示す。
LLM_UNAVAILABLE_ERROR = (
    "現在、AIによる判定を利用できません。時間をおいて、もう一度お試しください。"
    "不安なときは、警察相談専用ダイヤル「#9110」や"
    "消費者ホットライン「188（いやや）」にご相談ください。"
)

# 1日の上限に達したときの案内。判定を断る場面なので、再開時刻だけでなく
# 相談先も示す（危険な求人を前にした人を、案内なしで締め出さない）。
def daily_quota_error() -> str:
    """個人の上限に達したときの案内。

    件数は設定から取るため、読み込み時ではなく呼ばれた時に組み立てる。
    """
    return (
        f"本日の判定は上限（1日{quota.person_limit()}件）に達しました。"
        "日付が変わる（0時）と、また使えるようになります。"
        "不安なときは、警察相談専用ダイヤル「#9110」や"
        "消費者ホットライン「188（いやや）」にご相談ください。"
    )

# サイト全体の枠を使い切ったときの案内。個人の上限と混同されないよう、
# 「自分の使いすぎではない」ことが分かる書き方にする。
SITE_QUOTA_ERROR = (
    "本日ぶんの判定枠（サイト全体）が埋まりました。"
    "日付が変わる（0時）と、また使えるようになります。"
    "不安なときは、警察相談専用ダイヤル「#9110」や"
    "消費者ホットライン「188（いやや）」にご相談ください。"
)


def _image_discarded_by_memory_limit(request) -> bool:
    """アップロード画像が、フォームに届く前に捨てられた状態かどうか。

    FILE_UPLOAD_HANDLERS をメモリのみに固定しているため、リクエスト全体が
    FILE_UPLOAD_MAX_MEMORY_SIZE を超えると MemoryFileUploadHandler は自身を
    無効化する。後続のハンドラが居ないので、ファイル部分は request.FILES に
    載らないまま読み捨てられ、フォームには空の画像フィールドが渡る。

    ファイル以外のフィールドは DATA_UPLOAD_MAX_MEMORY_SIZE（既定 2.5MB）で
    別に制限され、超えればここに来る前に 413 になる。したがって multipart の
    リクエストが FILE_UPLOAD_MAX_MEMORY_SIZE を超えていて FILES が空なら、
    超過分はファイル部分＝捨てられた画像とみなせる。
    （両上限の大小関係は apps/judge/tests.py で検証している）
    """
    if not request.content_type.startswith("multipart/form-data"):
        return False
    if request.FILES:
        return False
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return False
    return content_length > settings.FILE_UPLOAD_MAX_MEMORY_SIZE


# エラー報告（DEBUG=True 時の 500 ページ、ADMINS 設定時の管理者メール）に
# POST の求人テキストが載らないようにする。
# あわせて、判定結果のページがブラウザやプロキシのキャッシュに保存されないよう
# no-store を返す。履歴と「戻る」操作までは防げない点に注意。
@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class IndexView(FormView):
    template_name = "judge/index.html"
    form_class = JobOfferRiskAssessForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.request.method in ("POST", "PUT"):
            kwargs["image_discarded"] = _image_discarded_by_memory_limit(self.request)
        return kwargs

    def get(self, request, *args, **kwargs):
        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(
                request,
                "現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。",
            )
        return super().get(request, *args, **kwargs)

    # 例外レポートのローカル変数一覧に求人テキスト・画像が載らないようにする
    @sensitive_variables()
    def form_valid(self, form):
        # 上限に達している場合は、API を叩かずに案内だけ返す
        if quota.is_exhausted(self.request):
            # 入力内容は残さないため、件数以外は出さない
            logger.info("Daily quota reached")
            messages.error(self.request, daily_quota_error())
            return self.render_to_response(self.get_context_data(form=form))

        # 全体の枠を確保する。取れなければ API は叩かない。個人の枠と違い、
        # 判定を返せたかどうかではなく「API に投げるか」で数える
        # （拒否や打ち切りでも、出力ぶんの費用は出ているため）。
        if not settings.VIEW_TEST_MODE and not quota.reserve_site_slot():
            logger.warning("Site-wide daily limit reached")
            messages.error(self.request, SITE_QUOTA_ERROR)
            return self.render_to_response(self.get_context_data(form=form))

        # キーの有無ではなく「値」で分岐する
        image = form.cleaned_data.get("image")
        text = form.cleaned_data.get("text")

        if image:
            job_offer_data = Image.open(image)  # PIL.Image.Image
        else:
            job_offer_data = text  # str

        # 型判別は job_offer_risk_assess 内の isinstance に任せて、そのまま渡す
        result = job_offer_risk_assess(job_offer_data)

        # 判定できなかった場合、サービス層は結果の代わりに理由を返す。
        # どちらも結果は表示せず、理由に応じた案内を出す。
        if result is None or isinstance(result, AssessmentError):
            if result is AssessmentError.UNAVAILABLE:
                # LLM に判定させること自体ができていない状態。
                logger.error("Risk assessment unavailable: no judgment from the LLM")
                messages.error(self.request, LLM_UNAVAILABLE_ERROR)
            else:
                logger.error("Risk assessment failed: service returned %r", result)
                messages.error(
                    self.request,
                    "判定に失敗しました。時間をおいて、もう一度お試しください。",
                )
            return self.render_to_response(self.get_context_data(form=form))

        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(
                self.request,
                "現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。",
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
