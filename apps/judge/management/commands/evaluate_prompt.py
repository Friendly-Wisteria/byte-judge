"""判定プロンプト・モデルの評価を回すコマンド。

    uv run python manage.py evaluate_prompt --dry-run   # API を叩かず構成だけ確認
    uv run python manage.py evaluate_prompt             # 実行（費用がかかる）

手動実行を前提にしている。CI で毎回走らせると、実行のたびに API の費用が出る。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.judge import service
from apps.judge.evalset import dataset, metrics
from apps.judge.quota import JST

# 1件あたりの費用（実測）。見積もりを出して、実行前に止まれるようにするため。
COST_PER_CASE_USD = 0.012

# グレーだけ既定で繰り返す。判定がぶれやすいのがこのカテゴリで、
# 全カテゴリを繰り返すと費用が件数ぶん増えるため。
DEFAULT_GRAY_REPEAT = 3


class Command(BaseCommand):
    help = "評価用テストセットで、判定プロンプトの精度を測る（API の費用がかかる）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--cases", type=Path, default=None, help="テストセットの TOML のパス"
        )
        parser.add_argument(
            "--category",
            action="append",
            choices=sorted(dataset.CATEGORIES),
            help="対象カテゴリを絞る（複数指定可）",
        )
        parser.add_argument("--limit", type=int, help="各カテゴリの上限件数")
        parser.add_argument(
            "--repeat", type=int, help="全カテゴリの繰り返し回数（安定性の測定用）"
        )
        parser.add_argument(
            "--gray-repeat",
            type=int,
            default=DEFAULT_GRAY_REPEAT,
            help=f"グレーだけの繰り返し回数（既定 {DEFAULT_GRAY_REPEAT}）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="API を叩かず、テストセットの構成と見積もりだけ出す",
        )
        parser.add_argument("--json", type=Path, help="結果を JSON でも保存する")
        parser.add_argument(
            "--yes", action="store_true", help="実行前の確認を省く"
        )

    def handle(self, *args, **options):
        try:
            cases = dataset.load_cases(options["cases"])
        except dataset.DatasetError as e:
            raise CommandError(str(e)) from e

        cases = self._select(cases, options)
        if not cases:
            raise CommandError("対象のケースがありません")

        plan = [(case, self._repeats(case, options)) for case in cases]
        runs = sum(n for _, n in plan)

        self._show_plan(cases, runs)
        if options["dry_run"]:
            self.stdout.write("\n--dry-run のため、API は呼び出していません。")
            return

        # 表示確認モードのままだと固定サンプルが返り、評価が意味を失う
        if settings.VIEW_TEST_MODE:
            raise CommandError(
                "VIEW_TEST_MODE=True では評価できません（固定サンプルが返るため）。"
                ".env の VIEW_TEST_MODE を False にしてください。"
            )

        if not options["yes"] and not self._confirm(runs):
            self.stdout.write("中止しました。")
            return

        outcomes = self._run(plan)
        report = self._report(cases, outcomes, runs)
        self.stdout.write(report)

        if options["json"]:
            self._write_json(options["json"], cases, outcomes)

    # ---- 準備 -------------------------------------------------------------

    def _select(self, cases, options):
        if options["category"]:
            wanted = set(options["category"])
            cases = [c for c in cases if c.category in wanted]
        if options["limit"]:
            kept, seen = [], {}
            for case in cases:
                seen[case.category] = seen.get(case.category, 0) + 1
                if seen[case.category] <= options["limit"]:
                    kept.append(case)
            cases = kept
        return cases

    def _repeats(self, case, options) -> int:
        if options["repeat"]:
            return max(1, options["repeat"])
        if case.category == "gray":
            return max(1, options["gray_repeat"])
        return 1

    def _show_plan(self, cases, runs):
        self.stdout.write("=" * 62)
        self.stdout.write("評価用テストセット")
        self.stdout.write("=" * 62)
        for name, count in dataset.composition(cases).items():
            if count:
                self.stdout.write(f"  {name:12} {count:3d}件  {dataset.CATEGORIES[name]}")
        self.stdout.write(f"\n  ケース数 {len(cases)} / 判定の実行回数 {runs}")
        self.stdout.write(
            f"  費用の見積もり 約 ${runs * COST_PER_CASE_USD:.2f}"
            f"（実測 ${COST_PER_CASE_USD}/件 × {runs}）"
        )

    def _confirm(self, runs) -> bool:
        answer = input(
            f"\n{runs} 回の判定を実行します（約 ${runs * COST_PER_CASE_USD:.2f}）。続けますか? [y/N] "
        )
        return answer.strip().lower() in ("y", "yes")

    # ---- 実行 -------------------------------------------------------------

    def _run(self, plan):
        outcomes = []
        done, total = 0, sum(n for _, n in plan)
        # 端末なら1行を上書きし、ファイルへ流すときは1行ずつ残す
        # （52件で20分ほどかかるため、リダイレクトして眺めることがある）
        interactive = getattr(self.stdout, "isatty", lambda: False)()
        started = time.monotonic()
        for case, repeats in plan:
            for nth in range(repeats):
                done += 1
                self.stdout.write(
                    f"[{done}/{total}] {case.id} ({case.category})"
                    + (f" {nth + 1}回目" if repeats > 1 else ""),
                    ending="\r" if interactive else "\n",
                )
                self.stdout.flush()
                outcomes.append(self._judge(case))
        self.stdout.write(f"\n実行時間 {self._duration(time.monotonic() - started)}\n")
        return outcomes

    def _duration(self, seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        return f"{int(seconds // 60)} 分 {int(seconds % 60):02d} 秒"

    def _judge(self, case):
        result = service.job_offer_risk_assess(case.text)
        if isinstance(result, service.AssessmentError):
            return metrics.Outcome(
                case_id=case.id, category=case.category, error=result.name
            )
        return metrics.outcome_from_report(case, result)

    # ---- 出力 -------------------------------------------------------------

    def _report(self, cases, outcomes, runs) -> str:
        fn = metrics.false_negative(outcomes)
        under = metrics.underrated_obvious(outcomes)
        fp = metrics.false_positive(outcomes)
        stab = metrics.stability(outcomes)
        gray = metrics.stability(outcomes, category="gray")
        recall = metrics.signal_recall(cases, outcomes)
        errs = metrics.errors(outcomes)

        lines = [
            "",
            "=" * 62,
            "評価結果",
            "=" * 62,
            f"  日時       {datetime.now(JST).isoformat(timespec='seconds')}",
            f"  モデル     {settings.CLAUDE_MODEL}",
            f"  ケース数   {len(cases)}（判定 {runs} 回 / エラー {sum(errs.values())} 回）",
            "",
            "-- 1. 危険求人の見逃し率（最重視）" + "-" * 26,
            f"  見逃し率   {fn.format()}",
        ]
        for label, count in fn.detail.items():
            lines.append(f"    - {label}: {count}件")
        lines += [
            "",
            "-- 2. グレー求人への判定の安定性 " + "-" * 27,
            f"  {gray.format()}",
        ]
        split = gray.split_cases
        if split:
            lines.append(f"    判定が割れたケース: {', '.join(split)}")
        lines += [
            "",
            "-- 3. シグナル理由の妥当性 " + "-" * 33,
            f"  期待した根拠の再現率 {recall.format()}",
            "  （キーワードの照合による目安。根拠の質そのものは測っていない）",
        ]
        for case_id, missed in list(recall.detail.items())[:10]:
            lines.append(f"    - {case_id}: 挙がらなかった根拠 {', '.join(missed)}")
        lines += [
            "",
            "-- 参考値 " + "-" * 50,
            f"  明らかな闇バイトを要注意止まり  {under.format()}",
            f"  普通の求人を危険と判定（偽陽性） {fp.format()}",
        ]
        if stab.cases > gray.cases:
            lines.append(f"  全体の安定性 {stab.format()}")
        if errs:
            lines.append("  判定できなかった理由: " + ", ".join(
                f"{name} {count}回" for name, count in errs.items()
            ))
        lines.append("=" * 62)
        return "\n".join(lines)

    def _write_json(self, path: Path, cases, outcomes):
        fn = metrics.false_negative(outcomes)
        gray = metrics.stability(outcomes, category="gray")
        recall = metrics.signal_recall(cases, outcomes)
        payload = {
            "run_at": datetime.now(JST).isoformat(timespec="seconds"),
            "model": settings.CLAUDE_MODEL,
            "cases": len(cases),
            "runs": len(outcomes),
            "false_negative_rate": fn.rate,
            "false_negative_detail": fn.detail,
            "gray_label_agreement": gray.label_agreement,
            "gray_score_stdev": gray.score_stdev,
            "gray_split_cases": gray.split_cases,
            "signal_recall": recall.rate,
            "underrated_obvious_rate": metrics.underrated_obvious(outcomes).rate,
            "false_positive_rate": metrics.false_positive(outcomes).rate,
            "errors": dict(metrics.errors(outcomes)),
            "outcomes": [
                {
                    "case_id": o.case_id,
                    "category": o.category,
                    "error": o.error,
                    "level": str(o.level) if o.level else None,
                    "label": o.label,
                    "score": o.score,
                    "has_enough_info": o.has_enough_info,
                }
                for o in outcomes
            ],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.stdout.write(f"\nJSON を書き出しました: {path}")
