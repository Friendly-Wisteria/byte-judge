from django.views.generic import FormView
from django.contrib import messages
from .forms import ScreenshotForm
from .service import job_offer_risk_assess
from config import settings
from PIL import Image


# Create your views here.
class IndexView(FormView):
    template_name = 'judge/index.html'
    form_class = ScreenshotForm
    def get(self,request,*args,**kwargs):
        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(request,'現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。')
        return super().get(request,*args,**kwargs)

    def form_valid(self,form):
        # 画像の読み込み
        uploaded_image = form.cleaned_data['image']
        image = Image.open(uploaded_image)
        # Gemini APIを叩く
        result = job_offer_risk_assess(image)

        # view test modeの時の警告表示
        if settings.VIEW_TEST_MODE:
            messages.warning(self.request, '現在、LLMによる判定を中止しています。表示される判定結果は使用しないでください。')

        return self.render_to_response(
            self.get_context_data(form=form,result=result)
        )
