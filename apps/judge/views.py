import logging

from django.conf import settings
from django.contrib import messages
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.generic import FormView
from PIL import Image

from .forms import JobOfferRiskAssessForm
from .service import job_offer_risk_assess

logger = logging.getLogger(__name__)


# エラー報告（DEBUG=True 時の 500 ページ、ADMINS 設定時の管理者メール）に
# POST の求人テキストが載らないようにする。
@method_decorator(sensitive_post_parameters(), name="dispatch")
class IndexView(FormView):
    template_name = "judge/index.html"
    form_class = JobOfferRiskAssessForm

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
