"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.urls import include, path

urlpatterns = [
    path("", include("apps.judge.urls")),
]

# Django 既定の英語ページの差し替え（#50）。判定を返せないときに相談先を必ず
# 添える約束を、view に届かない経路にも広げる。
#
# 403（CSRF 検証の失敗）は handler では差し替えられない。csrf_failure が
# 403_csrf.html を探すので、テンプレートを置くだけで切り替わる。
# 500 は apps/judge/templates/500.html が同じ役割を持つ（#45）。
handler400 = "apps.judge.views.bad_request"
