import base64
import io
import logging
import random
import re
from datetime import datetime
from enum import Enum
from pathlib import Path

import anthropic
import pydantic
from django.apps import apps
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from PIL import Image, ImageOps

from .fixtures import FIXTURES
from .quota import JST
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

# 求人テキストを <job_offer> で囲んで渡すため、テキスト側に同じタグが
# 現れると囲みの境界を偽装できてしまう（例: 本文中で </job_offer> を
# 閉じてから、その外側に命令文を書く）。このタグだけを対象に、
# 大文字小文字とタグ内の空白の揺れも含めて拾う。
_JOB_OFFER_TAG_RE = re.compile(r"<\s*(/?)\s*job_offer\s*>", re.IGNORECASE)

logger = logging.getLogger(__name__)

# トークン使用量だけを流すロガー。settings 側で標準出力に向けている。
usage_logger = logging.getLogger("apps.judge.usage")

# 入力トークン数を丸める単位。input_tokens は貼り付けられた求人文の長さの
# 近似値になる（判定プロンプトはキャッシュされ cache_read 側に回るため、
# 2回目以降はほぼ求人文の分だけになる）。費用の見積もりには足りる粒度まで
# 落として、個々の入力の長さが残らないようにする。
INPUT_TOKEN_BUCKET = 100


class AssessmentError(Enum):
    """判定結果を返せなかった理由。RiskReportSchema の代わりに返す。

    UNAVAILABLE: API に依頼した上で判定を得られなかった場合。レート制限、
        月額の利用上限による停止、API 障害・通信断、安全機構による拒否など、
        ユーザーが入力を直しても解消しない。呼び出し側では「今は判定を使えない」
        ことと、相談先（#9110・188）を案内する。
    FAILED: それ以外の失敗。入力の型違い、プロンプトの読み込み失敗、画像変換の
        失敗、応答がスキーマを満たさない・途中で切れた場合など。
    """

    UNAVAILABLE = "unavailable"
    FAILED = "failed"


def _escape_job_offer_tags(text: str) -> str:
    """求人テキスト内の <job_offer> / </job_offer> だけを無害化する。

    タグとして解釈されない形（&lt;job_offer&gt;）に置き換える。文字列
    自体は残るので、求人本文の意味は変わらず判定にも影響しない。
    他のタグや、タグ以外の '<' はそのまま残す。
    """
    return _JOB_OFFER_TAG_RE.sub(lambda m: f"&lt;{m.group(1)}job_offer&gt;", text)


def _silence_sdk_payload_logging():
    """anthropic SDK が送信ペイロード全文をログに出さないようにする。

    環境変数 ANTHROPIC_LOG=debug が設定されると、SDK は送信内容
    （求人テキスト・画像の base64）を "Request options" として DEBUG ログに
    出力する。環境変数ひとつで「入力内容を残さない」約束が崩れないよう、
    SDK ロガーを INFO 未満に下げさせない。

    SDK 側のレベル設定は anthropic の import 時に済んでいるため、
    この呼び出し（import より後）で上書きできる。
    """
    sdk_logger = logging.getLogger("anthropic")
    if sdk_logger.getEffectiveLevel() < logging.INFO:
        sdk_logger.setLevel(logging.INFO)


_silence_sdk_payload_logging()


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


def _as_int(value) -> int:
    """トークン数を整数にそろえる。読み取れない場合は 0。

    SDK が None を返す項目（cache_read_input_tokens など）があるため。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _log_token_usage(response) -> None:
    """トークン使用量を、標準出力に1行だけ残す（費用の把握に使う）。

    DB には保存しない。README の「保存するのは1日の判定件数だけ」という
    記述を変えずに済ませるため。

    入力文字数は記録しない。入力内容に由来する情報であり、「入力内容は
    一切残さない」と言い切れる状態を優先する。input_tokens も同じ理由で
    そのままは残さず、INPUT_TOKEN_BUCKET 単位に丸めて出す（キー名を
    input_100 にして、丸めた値だと分かるようにしている）。

    cache_read と cache_write は丸めない。どちらも固定の判定プロンプトの
    分量であって、利用者の入力には由来しないため。この2つが揃って初めて
    プロンプトキャッシュの命中率が分かる。命中するかどうかで1件あたりの
    費用が2倍以上変わるので、上限（SITE_DAILY_LIMIT）を決めるのに要る。

    時刻は時単位に丸める。秒まで残すと、判定した時刻から利用者をたどれる
    余地が残るため。書式側でも %(asctime)s を使わない。

    判定を返せたかどうかにかかわらず、応答が返った時点で呼ぶ。拒否や打ち切り
    でも出力ぶんの費用は出ているため、費用の把握としてはそれが正しい。
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        hour = datetime.now(JST).replace(minute=0, second=0, microsecond=0)
        # 四捨五入で丸める。切り捨てだと合計が常に少なめに出て、費用の
        # 見積もりがずれていくため。
        half = INPUT_TOKEN_BUCKET // 2
        input_tokens = _as_int(getattr(usage, "input_tokens", 0))
        input_bucket = (
            (input_tokens + half) // INPUT_TOKEN_BUCKET * INPUT_TOKEN_BUCKET
        )
        usage_logger.info(
            "token_usage hour=%s model=%s input_%s=%s output=%s cache_read=%s"
            " cache_write=%s",
            hour.isoformat(timespec="hours"),
            response.model,
            INPUT_TOKEN_BUCKET,
            input_bucket,
            _as_int(getattr(usage, "output_tokens", 0)),
            _as_int(getattr(usage, "cache_read_input_tokens", 0)),
            _as_int(getattr(usage, "cache_creation_input_tokens", 0)),
        )
    except Exception:  # noqa: BLE001
        # 使用量のログは費用把握のための付随情報にすぎない。ここでの失敗が
        # 判定の成否を変えてはいけないので、握りつぶして先に進む。
        # （呼び出し元の try の中にいるため、投げると API 障害として扱われ、
        #   判定できたはずの結果が捨てられてしまう）
        logger.warning("Could not log token usage")


@sensitive_variables()
def job_offer_risk_assess(job_offer) -> RiskReportSchema | AssessmentError:
    """
    求人の画像をClaude APIに投げて、闇バイトへの関与のリスク度合いを評価する
    Args:
        job_offer (Image): 求人のスクリーンショット
    Returns:
        リスクアセスの結果 (RiskReportSchema)。
        判定できなかった場合は、その理由 (AssessmentError)
    """
    if not isinstance(job_offer, Image.Image) and not isinstance(job_offer, str):
        logger.error("Type Error: job_offer must be image or text")
        return AssessmentError.FAILED

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
        with open(PROMPT_PATH, encoding="utf-8") as f:
            prompt_text = f.read()
    except FileNotFoundError:
        logger.error("Error: Prompt template file not found %s", PROMPT_PATH)
        return AssessmentError.FAILED

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
            return AssessmentError.FAILED
    else:
        # マークダウン等をそのまま渡せるよう求人を <job_offer> で囲む。
        # テキスト側の同名タグは、境界の偽装に使えないよう無害化しておく。
        safe_job_offer = _escape_job_offer_tags(job_offer)
        content = [
            {
                "type": "text",
                "text": f"# 評価対象の求人\n<job_offer>\n{safe_job_offer}\n</job_offer>",
            }
        ]

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
        logger.info("Claude Model: %s", response.model)
        _log_token_usage(response)
    except anthropic.NotFoundError:
        # モデルID誤りなど。設定ミスで再試行しても回復しないが、
        # ユーザーから見れば「判定を受けられない」状態に変わりはない。
        logger.error("Unknown Claude model: %s", settings.CLAUDE_MODEL)
        return AssessmentError.UNAVAILABLE
    except anthropic.RateLimitError:
        # SDKが自動リトライした上でなお超過している状態。
        logger.error("Claude API rate limited")
        return AssessmentError.UNAVAILABLE
    except anthropic.APIStatusError as e:
        # API 由来のエラー（4xx / 5xx など）。月額の利用上限に達して止まった場合も
        # ここに来る（課金起因は 402 billing_error、混雑や超過は 429 / 5xx と、
        # 状況でステータスが変わる）。いずれもユーザー側では解消できないため
        # 区別せず扱い、原因の切り分けはログに残したステータスで行う。
        logger.error(
            "Claude API error: %s %s: %s",
            e.status_code,
            e.type,
            e.message,
            exc_info=False,
        )
        return AssessmentError.UNAVAILABLE
    except anthropic.APIConnectionError:
        # ネットワーク断・タイムアウト。詳細はログのみに残す。
        logger.exception("Network error while requesting Claude API")
        return AssessmentError.UNAVAILABLE
    except pydantic.ValidationError as e:
        # LLMの出力がスキーマを満たさない（必須項目が空など）。
        # 部分的な結果を画面に出さず、確実にエラーへ倒す。
        # ValidationError の文字列表現には不適合だった値そのもの（求人文を
        # 引用した summary など）が input_value として含まれるため、トレースは
        # 出さず、「どの項目がどの理由で落ちたか」だけを残す。
        logger.error(
            "Response did not satisfy RiskReportSchema: %s",
            [
                (list(d["loc"]), d["type"])
                for d in e.errors(
                    include_input=False, include_url=False, include_context=False
                )
            ],
        )
        return AssessmentError.FAILED
    except Exception:
        # 想定外の例外。詳細はログのみに残す。
        logger.exception("Unexpected error while requesting Claude API")
        return AssessmentError.FAILED

    # 5. 安全機構による拒否・出力打ち切りの検出
    #    闇バイト＝犯罪関連の文言を扱うため、稀に安全機構が発火する。
    #    この場合 stop_reason が refusal になり、内容は空またはスキーマ不適合になる。
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        logger.error("Claude refused the request (category=%s)", category)
        return AssessmentError.UNAVAILABLE
    if response.stop_reason == "max_tokens":
        logger.error("Response truncated: max_tokens (%s) reached", MAX_TOKENS)
        return AssessmentError.FAILED

    # 6. レスポンスを、RiskReportSchemaでパースして出力
    # 6-1. パースできない場合はエラー
    logger.info("Parsing response...")
    if response.parsed_output is None:
        logger.error("Parse error")
        return AssessmentError.FAILED

    return response.parsed_output
