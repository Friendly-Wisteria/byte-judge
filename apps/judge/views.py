from django.views.generic import FormView

from .forms import ScreenshotForm
from .schema import RiskReportSchema
from .tests import FIXTURES
from .service import job_offer_risk_assess
from config import settings

import random

from PIL import Image


# Create your views here.
class IndexView(FormView):
    template_name = 'judge/index.html'
    form_class = ScreenshotForm

    def form_valid(self,form):
        uploaded_image = form.cleaned_data['image']

        if settings.VIEW_TEST_MODE:
            """
            VIEW TEST MODE
            Gemini APIを叩かず、オフラインで完結するテストのみを実行
            """
            case = random.choice(list(FIXTURES))
            result = RiskReportSchema.model_validate(FIXTURES[case])
        else:
            """
            本番モード
            """
            image = Image.open(uploaded_image)
            result = job_offer_risk_assess(image) # Gemini APIを叩く

        return self.render_to_response(
            self.get_context_data(form=form,result=result)
        )
