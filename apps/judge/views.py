import logging

from django.conf import settings
from django.contrib import messages
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.generic import FormView
from PIL import Image

from .forms import JobOfferRiskAssessForm
from .service import job_offer_risk_assess

logger = logging.getLogger(__name__)


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
        # キーの有無ではなく「値」で分岐する
        image = form.cleaned_data.get("image")
        text = form.cleaned_data.get("text")

        if image:
            job_offer_data = Image.open(image)  # PIL.Image.Image
        else:
            job_offer_data = text  # str

        # 型判別は job_offer_risk_assess 内の isinstance に任せて、そのまま渡す
        result = job_offer_risk_assess(job_offer_data)

        # 判定に失敗した場合（型エラー / API エラー / パース失敗など）は
        # サービス層が None を返す。結果は表示せず、エラーメッセージを提示する。
        if result is None:
            logger.error("Risk assessment failed: service returned None")
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

        return self.render_to_response(self.get_context_data(form=form, result=result))

    def form_invalid(self, form):
        # サーバ側バリデーションのエラーを messages に載せてテンプレートで表示
        for errors in form.errors.values():
            for error in errors:
                messages.error(self.request, error)
        return super().form_invalid(form)
