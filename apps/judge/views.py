from django.views.generic import FormView
from django.contrib import messages
from .forms import JobOfferRiskAssessForm
from .service import job_offer_risk_assess
from django.conf import settings
from PIL import Image
import logging

logger = logging.getLogger(__name__)


# Create your views here.
class IndexView(FormView):
    template_name = 'judge/index.html'
    form_class = JobOfferRiskAssessForm
    def get(self,request,*args,**kwargs):
        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(request,'現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。')
        return super().get(request,*args,**kwargs)

    def form_valid(self,form):
        # キーの有無ではなく「値」で分岐する
        image = form.cleaned_data.get('image')
        text = form.cleaned_data.get('text')

        if image:
            job_offer_data = Image.open(image)   # PIL.Image.Image
        else:
            job_offer_data = text                # str

        logger.info(f'data format: {type(job_offer_data)}')

        # 型判別は job_offer_risk_assess 内の isinstance に任せて、そのまま渡す
        result = job_offer_risk_assess(job_offer_data)

        # 判定に失敗した場合（型エラー / API エラー / パース失敗など）は
        # サービス層が None を返す。結果は表示せず、エラーメッセージを提示する。
        if result is None:
            logger.error('Risk assessment failed: service returned None')
            messages.error(self.request, '判定に失敗しました。時間をおいて、もう一度お試しください。')
            return self.render_to_response(
                self.get_context_data(form=form)
            )

        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(self.request, '現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。')

        return self.render_to_response(
            self.get_context_data(form=form, result=result)
        )

    def form_invalid(self, form):
        # サーバ側バリデーションのエラーを messages に載せてテンプレートで表示
        for errors in form.errors.values():
            for error in errors:
                messages.error(self.request, error)
        return super().form_invalid(form)
