# Cloud Run 用。uv でロックした依存をそのまま入れ、gunicorn で待ち受ける。
#
# 静的ファイルの配信は入れていない（collectstatic も WhiteNoise も無い）。
# テンプレートは Bootswatch を CDN から読み、{% load static %} を使う箇所が
# 無く、django.contrib.admin も未導入のため、現時点で配信すべきファイルが
# 無いことによる。admin（#39）を入れるときに、ここもあわせて見直すこと。
FROM python:3.13-slim

# uv は公式イメージからバイナリだけを持ってくる（手元の 0.9.18 に合わせて固定）。
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

WORKDIR /app

# 依存だけを先に入れてレイヤーに残す（アプリのコードを直してもここは作り直さない）。
# このプロジェクトは uv.lock 上 virtual（パッケージではない）なので、
# uv sync で入るのは依存だけ。--locked は pyproject と uv.lock のズレを
# ビルド時に失敗として気づけるようにするため。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

# root で動かさない。書き込みは行わないので、ホームディレクトリも作らない。
RUN useradd --system --no-create-home appuser
USER appuser

# --timeout 0：判定は最長で数分かかる（service.py の timeout=180.0 +
#   max_retries=4）。gunicorn 側でも打ち切ると、どちらが切ったのか分からなく
#   なるため、打ち切りの判断は Cloud Run の --timeout に一本化する。
# --threads 8：待ち時間のほとんどは Claude API の応答待ちで CPU を使わない。
#   Cloud Run 側の --concurrency と同じ値に揃えること。
# アクセスログは出さない：リクエストの記録は Cloud Run 側に既にあり、
#   同じ内容（IP を含む）をアプリ側でもう一度残す理由が無い。
CMD exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --threads 8 \
    --timeout 0
