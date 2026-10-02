"""报告生成器 — HTML 报告、性能图表、差异明细"""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.testing.comparator import CompareResult
from app.testing.stress import StressMetrics


@dataclass
class TestReport:
    """测试报告"""
    title: str = ""
    created_at: str = ""
    summary: dict = field(default_factory=dict)
    stress_metrics: StressMetrics | None = None
    compare_results: list[CompareResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_html(self) -> str:
        """生成 HTML 报告"""
        items = []
        items.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
        items.append(f"<title>{self.title}</title>")
        items.append(self._style())
        items.append("</head><body>")

        # 标题
        items.append(f"<h1>{self.title}</h1>")
        items.append(f"<p class='timestamp'>生成时间: {self.created_at}</p>")

        # 概要
        items.append("<h2>测试概要</h2>")
        items.append("<table class='summary'>")
        for k, v in self.summary.items():
            items.append(f"<tr><td>{k}</td><td>{v}</td></tr>")
        items.append("</table>")

        # 压测结果
        if self.stress_metrics:
            items.append("<h2>压测结果</h2>")
            items.append(self._stress_table(self.stress_metrics))

        # 比对结果
        if self.compare_results:
            items.append("<h2>数据比对结果</h2>")
            items.append(self._compare_table(self.compare_results))

        # 错误
        if self.errors:
            items.append("<h2>错误列表</h2>")
            items.append("<ul class='errors'>")
            for e in self.errors:
                items.append(f"<li>{e}</li>")
            items.append("</ul>")

        items.append("</body></html>")
        return "\n".join(items)

    def to_json(self) -> str:
        """序列化为 JSON"""
        data = {
            "title": self.title,
            "created_at": self.created_at,
            "summary": self.summary,
        }
        if self.stress_metrics:
            data["stress"] = {
                "total_requests": self.stress_metrics.total_requests,
                "successful": self.stress_metrics.successful,
                "failed": self.stress_metrics.failed,
                "success_rate": self.stress_metrics.success_rate,
                "qps": self.stress_metrics.qps,
                "avg_latency_ms": self.stress_metrics.avg_latency_ms,
                "p50_latency_ms": self.stress_metrics.p50_latency_ms,
                "p95_latency_ms": self.stress_metrics.p95_latency_ms,
                "p99_latency_ms": self.stress_metrics.p99_latency_ms,
                "elapsed_ms": self.stress_metrics.elapsed_ms,
            }
        if self.compare_results:
            data["compare"] = {
                "total": len(self.compare_results),
                "matched": sum(1 for r in self.compare_results if r.matched),
                "mismatched": sum(1 for r in self.compare_results if not r.matched),
                "details": [
                    {
                        "matched": r.matched,
                        "match_rate": r.match_rate,
                        "diffs": [{"field": d.field, "expected": str(d.expected),
                                   "actual": str(d.actual), "reason": d.reason}
                                  for d in r.diffs],
                    }
                    for r in self.compare_results
                ],
            }
        if self.errors:
            data["errors"] = self.errors
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _stress_table(m: StressMetrics) -> str:
        rows = [
            ("总请求数", m.total_requests),
            ("成功数", m.successful),
            ("失败数", m.failed),
            ("成功率", f"{m.success_rate:.2%}"),
            ("QPS", f"{m.qps:.1f}"),
            ("平均延迟", f"{m.avg_latency_ms:.1f}ms"),
            ("P50", f"{m.p50_latency_ms:.1f}ms"),
            ("P95", f"{m.p95_latency_ms:.1f}ms"),
            ("P99", f"{m.p99_latency_ms:.1f}ms"),
            ("最小延迟", f"{m.min_latency_ms:.1f}ms"),
            ("最大延迟", f"{m.max_latency_ms:.1f}ms"),
            ("总耗时", f"{m.elapsed_ms:.0f}ms"),
        ]
        parts = ["<table class='stress'>"]
        for label, value in rows:
            parts.append(f"<tr><td>{label}</td><td>{value}</td></tr>")
        parts.append("</table>")
        return "\n".join(parts)

    @staticmethod
    def _compare_table(results: list[CompareResult]) -> str:
        matched = sum(1 for r in results if r.matched)
        parts = [
            "<div class='compare-summary'>",
            f"总数: {len(results)}, 匹配: {matched}, 不匹配: {len(results) - matched}",
            "</div>",
            "<table class='compare'>",
            "<tr><th>序号</th><th>匹配</th><th>匹配率</th><th>差异</th></tr>",
        ]
        for i, r in enumerate(results):
            diffs_text = "; ".join(
                f"{d.field}: 期望={d.expected} 实际={d.actual}"
                for d in r.diffs[:3]  # 只展示前3个差异
            )
            cls = "pass" if r.matched else "fail"
            parts.append(
                f"<tr class='{cls}'><td>{i+1}</td>"
                f"<td>{'是' if r.matched else '否'}</td>"
                f"<td>{r.match_rate:.0%}</td><td>{diffs_text}</td></tr>"
            )
        parts.append("</table>")
        return "\n".join(parts)

    @staticmethod
    def _style() -> str:
        return """
        <style>
          body { font-family: sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #f8f9fa; }
          h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
          h2 { color: #34495e; margin-top: 30px; }
          .timestamp { color: #7f8c8d; font-size: 14px; }
          table { width: 100%; border-collapse: collapse; margin: 10px 0; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
          th { background: #3498db; color: #fff; padding: 10px; text-align: left; }
          td { padding: 8px 10px; border-bottom: 1px solid #eee; }
          tr.pass { background: #eafaf1; }
          tr.fail { background: #fdecea; }
          ul.errors { background: #fdecea; padding: 15px; border-radius: 4px; }
          ul.errors li { margin: 5px 0; color: #c0392b; }
          .compare-summary { background: #eaf2f8; padding: 10px; border-radius: 4px; margin: 10px 0; }
        </style>"""


class ReportGenerator:
    """报告生成器"""

    @staticmethod
    def create(title: str, stress_metrics: StressMetrics | None = None,
               compare_results: list[CompareResult] | None = None,
               errors: list[str] | None = None, summary: dict | None = None) -> TestReport:
        """创建测试报告"""
        return TestReport(
            title=title,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            summary=summary or {"报告状态": "完成"},
            stress_metrics=stress_metrics,
            compare_results=compare_results or [],
            errors=errors or [],
        )
