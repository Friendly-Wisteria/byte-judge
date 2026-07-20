from django.apps import apps
from google import genai
from PIL import Image
from google.genai import types,errors
from .schema import RiskReportSchema
from .tests import FIXTURES
from config import settings
from pathlib import Path
import io
import logging
import random

# Appのパスの取得
APP_PATH = apps.get_app_config('judge').path
PROMPT_PATH = Path(APP_PATH) / 'templates' / 'judge' / 'prompts' / 'job_offer_risk_assess.md'

logger = logging.getLogger(__name__)

def _pil_to_part(img: Image.Image) -> types.Part:
    """
    PIL画像を Gemini に渡せる inline画像 Part に変換する。
    スクショはテキストが重要なので、可逆なPNGで固定するのが無難。
    """
    buffer = io.BytesIO()
    img.save(buffer,format='PNG')
    return types.Part.from_bytes(data=buffer.getvalue(),mime_type="image/png")

def job_offer_risk_assess(job_offer:Image.Image)->RiskReportSchema:
    """
    求人の画像をGemini APIに投げて、闇バイトへの関与のリスク度合いを評価する
    Args:
        job_offer (Image): 求人のスクリーンショット
    Returns:
        リスクアセスの結果 (ReskReportSchema)
    """
    # 0. VIEW TEST MODEの場合、実際にはAPIを叩かず、サンプルを出力する
    if settings.VIEW_TEST_MODE:
        logger.warning('VIEW TEST MODE: Show only response sample, NOT an actual LLM response.')
        case = random.choice(list(FIXTURES))
        return RiskReportSchema.model_validate(FIXTURES[case])

    # 1. Gemini API Clientの構築
    logger.info('Creating Client...')
    # 1-1 API Client
    client = genai.Client(
        http_options=types.HttpOptions(
            timeout=180000,
            retry_options=types.HttpRetryOptions(
                attempts=4,
                initial_delay=10.0,
                max_delay=120.0,
                exp_base=2.0
            )
        )
    )
    # 1-2. API Client Config
    logger.info('Creating config...')
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RiskReportSchema
    )
    # 2. プロンプトの定型文の読み込み
    logger.info('Loading prompt template...')
    try:
        with open(PROMPT_PATH,'r',encoding='utf-8') as f:
            prompt_text = f.read()
    except FileNotFoundError:
        logging.error(f"Error: Prompt template file not found {PROMPT_PATH}")
        return None

    # 3. Gemini APIを叩く
    logger.info('Requesting to Gemini API...')
    contents = [prompt_text,_pil_to_part(job_offer)]
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=contents,
            config=config
        )
    except errors.ServerError as e:
        logger.error(f"Server error:{e.code} {e.status}: {e.message}",exc_info=False)
        return None
    # 4. レスポンスを、RiskReportSchemaでパースして出力
    # 4-1. パースできない場合はエラー
    logger.info('Parsing response...')
    if not response.parsed:
        logger.error("Parse error")
        return None

    return response.parsed
