"""1日あたりの判定回数の上限を、署名付き Cookie だけで管理する。

「サーバー側に何も保存しない」という本アプリの方針を崩さないため、カウントは
ブラウザ側の Cookie に日付と件数だけを持たせ、改ざんは SECRET_KEY 由来の署名で
検知する（DB もセッションも増やさない）。

Cookie を消す・別のブラウザを使えば回避できるが、回避はいずれも「枠が戻る」
方向にしか働かない。これは連打や軽い荒らしへの摩擦であり、費用面の歯止めは
Anthropic 側の月額上限（service.AssessmentError.UNAVAILABLE）に置いている。
"""

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum

from django.conf import settings
from django.core.signing import BadSignature
from django.db import DatabaseError, IntegrityError
from django.db.models import F

from .models import DailyUsage

logger = logging.getLogger(__name__)


def person_limit() -> int:
    """1人あたりの1日の判定回数。"""
    return int(getattr(settings, "PERSON_DAILY_LIMIT", 4))


# 全体の件数を残す日数。過去ぶんは運用の観測（1日に何件来ているか）に使う。
RETENTION_DAYS = 90

COOKIE_NAME = "bj_quota"
COOKIE_SALT = "apps.judge.quota"

# 利用者は日本国内を想定しているため、リセットは日本時間の0時に合わせる
# （settings.TIME_ZONE は UTC のままなので、ここで明示的に JST へ変換する）。
JST = timezone(timedelta(hours=9), "JST")


class SiteSlotReservationError(Enum):
    """サイト全体の枠を確保できなかった理由。確保できた場合は None を返す。

    DATABASE_ERROR: 件数を記録している DB に接続できなかった場合。
    DAILY_QUOTA_REACHED: すでに1日の上限に達している場合。
    """

    DATABASE_ERROR = "database_error"
    DAILY_QUOTA_REACHED = "daily_quota_reached"


def _now_jst() -> datetime:
    """現在時刻（JST）。テストではここだけを差し替える。"""
    return datetime.now(JST)


def _date_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _seconds_until_reset(now: datetime) -> int:
    """次の 0 時（JST）までの秒数。Cookie の寿命に使う。"""
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((tomorrow - now).total_seconds())


def used_today(request, now: datetime | None = None) -> int:
    """当日ぶんの判定回数。

    Cookie が無い / 壊れている / 署名が合わない / 日付が変わっている場合は 0。
    いずれも「使えなくなる」側ではなく「枠が戻る」側に倒す（利用者を誤って
    締め出さないため。回避可能なのは Cookie を消せば同じなので損失はない）。
    """
    now = now or _now_jst()
    try:
        raw = request.get_signed_cookie(COOKIE_NAME, default=None, salt=COOKIE_SALT)
    except BadSignature:
        return 0
    if not raw:
        return 0

    date_key, _, count = raw.partition(":")
    if date_key != _date_key(now):
        return 0
    try:
        return max(0, int(count))
    except ValueError:
        return 0


def is_exhausted(request, now: datetime | None = None) -> bool:
    return used_today(request, now) >= person_limit()


def consume(request, response) -> None:
    """判定 1 件ぶんを消費し、カウントを Cookie に書き戻す。"""
    now = _now_jst()
    used = min(used_today(request, now) + 1, person_limit())
    response.set_signed_cookie(
        COOKIE_NAME,
        f"{_date_key(now)}:{used}",
        salt=COOKIE_SALT,
        max_age=_seconds_until_reset(now),
        httponly=True,
        samesite="Lax",
        secure=not settings.DEBUG,
    )


# --- ここから、サイト全体の1日の上限 -------------------------------------
# 個人の枠（上）は連打への摩擦だが、こちらは費用の歯止め。月額の利用上限を
# 1日で使い切られると、翌月まで全員が判定を受けられなくなるため、被害を
# その日のうちに閉じ込める。


def site_limit() -> int:
    return int(getattr(settings, "SITE_DAILY_LIMIT", 20))


def reserve_site_slot(
    now: datetime | None = None,
) -> tuple[bool, SiteSlotReservationError | None]:
    """全体の枠を 1 件ぶん確保する。取れなければ False。

    第二要素は取れなかった理由（確保できたときは None）。

    個人の枠と違い「API に投げた回数」を数える。拒否や打ち切りでも出力ぶんの
    費用は出ているため、費用の歯止めとしては投げた回数で数えるのが正しい。

    確保は UPDATE ... WHERE count < 上限 の 1 文で行うため、ワーカーが複数
    あっても上限を超えない。
    """
    now = now or _now_jst()
    today = now.date()
    reason = None
    try:
        try:
            _, created = DailyUsage.objects.get_or_create(date=today)
        except IntegrityError:
            # 別のワーカーが同時に作った。行はあるので続行する。
            created = False

        if created:
            DailyUsage.objects.filter(
                date__lt=today - timedelta(days=RETENTION_DAYS)
            ).delete()

        is_reserved = bool(
            DailyUsage.objects.filter(date=today, count__lt=site_limit()).update(
                count=F("count") + 1
            )
        )
        if not is_reserved:
            logger.warning("Site-wide daily limit reached")
            reason = SiteSlotReservationError.DAILY_QUOTA_REACHED
    except DatabaseError:
        logger.exception("Database error while reserving a site slot")
        reason = SiteSlotReservationError.DATABASE_ERROR
        is_reserved = False

    return is_reserved, reason
