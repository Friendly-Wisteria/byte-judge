import base64
import io
import logging
import random
from pathlib import Path

import anthropic
import pydantic
from django.apps import apps
from django.conf import settings
from PIL import Image, ImageOps

from .fixtures import FIXTURES
from .schema import RiskReportSchema

# Appのパスの取得
APP_PATH = apps.get_app_config("judge").path
PROMPT_PATH = (
    Path(APP_PATH) / "templates" / "judge" / "prompts" / "job_offer_risk_assess.md"
)

# 判定結果のJSON自体は小さいが、Claudeは既定で思考トークンを消費するため余裕を持たせる
MAX_TOKENS = 16000

# Claude の高解像度ティア（Claude 4.7 以降）の長辺上限。これを超える画像は
# API 側でどのみち自動縮小されるため、送信前に合わせる。
MAX_IMAGE_LONG_EDGE = 2576

logger = logging.getLogger(__name__)


def _pil_to_image_block(img: Image.Image) -> dict:
    """
    PIL画像を Claude に渡せる inline画像ブロックに変換する。
    スクショはテキストが重要なので、可逆なPNGで固定するのが無難。
    """
    # 1. EXIF の回転情報を反映（横倒しのまま送られると読み取り精度が落ちる）
    img = ImageOps.exif_transpose(img)
    # 2. PNGで保存できないモード（CMYK等）を正規化
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 3. 長辺を上限に合わせる。API側の自動縮小と同じ結果になるため判定精度は
    #    落ちず、送信サイズ（base64 10MB上限）とトークン数だけが下がる。
    if max(img.size) > MAX_IMAGE_LONG_EDGE:
        img.thumbnail((MAX_IMAGE_LONG_EDGE, MAX_IMAGE_LONG_EDGE), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(buffer.getvalue()).decode("utf-8"),
        },
    }


def job_offer_risk_assess(job_offer) -> RiskReportSchema:
    """
    求人の画像をClaude APIに投げて、闇バイトへの関与のリスク度合いを評価する
    Args:
        job_offer (Image): 求人のスクリーンショット
    Returns:
        リスクアセスの結果 (ReskReportSchema)
    """
    if not isinstance(job_offer, Image.Image) and not isinstance(job_offer, str):
        logger.error("Type Error: job_offer must be image or text")
        return None

    # 0. VIEW TEST MODEの場合、実際にはAPIを叩かず、サンプルを出力する
    if settings.VIEW_TEST_MODE:
        logger.warning(
            "VIEW TEST MODE: Show only response sample, NOT an actual LLM response."
        )
        case = random.choice(list(FIXTURES))
        return RiskReportSchema.model_validate(FIXTURES[case])

    # 1. Claude API Clientの構築
    #    APIキーは環境変数 ANTHROPIC_API_KEY から自動で読み込まれる
    logger.info("Creating Client...")
    client = anthropic.Anthropic(timeout=180.0, max_retries=4)

    # 2. プロンプトの読み込み
    logger.info("Loading prompt template...")
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt_text = f.read()
    except FileNotFoundError:
        logger.error(f"Error: Prompt template file not found {PROMPT_PATH}")
        return None

    # 3. 評価対象の組み立て
    #    プロンプトを system、求人を user に分けることで、求人内の文言が
    #    LLMへの命令として解釈されにくくなる（プロンプトインジェクション対策）
    if isinstance(job_offer, Image.Image):
        # 画像の変換は失敗し得る（破損ファイル・非対応モード等）。
        # ここで例外が漏れると views.py のエラー処理を経ずに500になる。
        try:
            content = [
                _pil_to_image_block(job_offer),
                {"type": "text", "text": "# 評価対象の求人"},
            ]
        except (OSError, ValueError):
            logger.exception("Failed to convert uploaded image")
            return None
    else:
        content = [{"type": "text", "text": f"# 評価対象の求人\n{job_offer}"}]

    # 4. Claude APIを叩く
    logger.info("Requesting to Claude API...")
    try:
        response = client.messages.parse(
            model=settings.CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": prompt_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_format=RiskReportSchema,
        )
        logger.info(f"Claude Model: {response.model}")
    except anthropic.NotFoundError:
        # モデルID誤りなど。設定ミスなので再試行しても回復しない。
        logger.error(f"Unknown Claude model: {settings.CLAUDE_MODEL}")
        return None
    except anthropic.RateLimitError:
        # SDKが自動リトライした上でなお超過している状態。
        logger.error("Claude API rate limited")
        return None
    except anthropic.APIStatusError as e:
        # API 由来のエラー（4xx / 5xx など）。
        # 詳細はログのみに残し、ユーザーには見せない（呼び出し側で汎用メッセージを表示）。
        logger.error(
            f"Claude API error: {e.status_code} {e.type}: {e.message}", exc_info=False
        )
        return None
    except anthropic.APIConnectionError:
        # ネットワーク断・タイムアウト。詳細はログのみに残す。
        logger.exception("Network error while requesting Claude API")
        return None
    except pydantic.ValidationError:
        # LLMの出力がスキーマを満たさない（必須項目が空など）。
        # 部分的な結果を画面に出さず、確実にエラーへ倒す。
        logger.error("Response did not satisfy RiskReportSchema", exc_info=True)
        return None
    except Exception:
        # 想定外の例外。詳細はログのみに残す。
        logger.exception("Unexpected error while requesting Claude API")
        return None

    # 5. 安全機構による拒否・出力打ち切りの検出
    #    闇バイト＝犯罪関連の文言を扱うため、稀に安全機構が発火する。
    #    この場合 stop_reason が refusal になり、内容は空またはスキーマ不適合になる。
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        logger.error(f"Claude refused the request (category={category})")
        return None
    if response.stop_reason == "max_tokens":
        logger.error(f"Response truncated: max_tokens ({MAX_TOKENS}) reached")
        return None

    # 6. レスポンスを、RiskReportSchemaでパースして出力
    # 6-1. パースできない場合はエラー
    logger.info("Parsing response...")
    if response.parsed_output is None:
        logger.error("Parse error")
        return None

    return response.parsed_output
