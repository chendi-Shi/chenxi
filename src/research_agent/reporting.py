from __future__ import annotations

import json
import re
import uuid
from pathlib import Path


def escape(text):
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", str(text)).replace("\n", " ")


def markdown(report: dict) -> str:
    lines = [
        "# 投研证据简报",
        "",
        f"运行：`{report['run_id']}`",
        f"状态：{report['status']} · 模式：{report['provider']} · 时间（UTC）：{report['created_at']}",
        f"模型：{escape(report['model'] or '规则基线，无模型调用')}",
        "",
        "> 本报告是证据整理稿，需人工复核。规则结果不能代表模型能力或真实研究效果。",
        "",
    ]
    metrics = report["metrics"]
    lines += [
        "## 处理范围",
        "",
        f"文档 {metrics['documents']} 份；分块 {metrics['chunks']} 个；"
        f"成功 {metrics['successful_chunks']} 个；缓存命中 {metrics['cache_hits']} 个；事实 {metrics['facts']} 条。",
        "",
    ]
    if report["failures"]:
        lines += ["## 未完成的处理", ""]
        for failure in report["failures"]:
            lines.append(
                f"- 文档 `{failure['document_id']}` / 分块 `{failure['chunk_id']}`：{escape(failure['error'])}"
            )
    if report["empty_chunks"] or report["saturated_chunks"]:
        lines += [
            "",
            f"覆盖率待复核：空结果 {len(report['empty_chunks'])} 块；达到事实条数上限 {len(report['saturated_chunks'])} 块。",
            "",
        ]
    if report["conflicts"]:
        lines += ["## 疑似口径冲突（待人工判断）", ""]
        for conflict in report["conflicts"]:
            lines.append(
                f"- {escape(conflict['entity'])} / {escape(conflict['period'])} / {escape(conflict['metric'])}："
                f"{escape(', '.join(conflict['values']))} {escape(conflict['unit'])}"
            )
    categories = list(dict.fromkeys(f["category"] for f in report["facts"]))
    for category in categories:
        lines += ["", f"## {category}", ""]
        for fact in (f for f in report["facts"] if f["category"] == category):
            lines += [
                f"### {escape(fact['summary'])}",
                "",
                f"事实 `{fact['id']}` · {fact['nature']} · {fact['importance']}",
                "",
            ]
            for evidence in fact["evidence"]:
                page = f" · 第 {evidence['page']} 页" if evidence["page"] else ""
                lines += [
                    f"> {escape(evidence['quote'])}",
                    "",
                    f"出处：文档 `{evidence['document_id']}`{page} · 字符 [{evidence['start']}, {evidence['end']})",
                    "",
                ]
    lines += ["## 来源目录", ""]
    for doc in report["documents"]:
        lines.append(f"### 文档 `{doc['id']}`")
        for source in doc["sources"]:
            lines.append(
                f"- {escape(source['title'])} / {escape(source['source'])} / {escape(source['published_at'] or '日期缺失')}"
            )
            if source["url"]:
                lines.append(f"  - 来源地址：{escape(source['url'])}")
        if doc["warnings"]:
            lines.append("- 解析提示：" + escape(", ".join(doc["warnings"])))
    lines += ["", "## 复核与边界", ""]
    for review in report.get("reviews", []):
        lines += [
            f"- {escape(review['reviewer'])} / {review['status']} / {review['created_at']}：{escape(review['note'])}"
        ]
    if not report.get("reviews"):
        lines += ["尚无人工作出复核记录。"]
    lines += [
        "",
        *[f"- {escape(text)}" for text in report["limitations"]],
        "- 处理耗时不含人工阅读和复核，不能直接视为节省工时。",
        "",
        "## 可复现信息",
        "",
        f"配置哈希：`{report['config_key']}`",
        f"提示词哈希：`{report['prompt_hash']}`",
        f"输入 tokens：{metrics['input_tokens']}；输出 tokens：{metrics['output_tokens']}；HTTP 尝试次数：{metrics['http_attempts']}。",
    ]
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_report(store, run_id: str, out: Path):
    record = store.get_run(run_id)
    if not record["report_json"]:
        raise ValueError("report_not_ready")
    report = json.loads(record["report_json"])
    with store.connection() as conn:
        report["reviews"] = [
            dict(row)
            for row in conn.execute(
                "SELECT reviewer,status,note,created_at FROM reviews WHERE run_id=? ORDER BY id",
                (run_id,),
            )
        ]
    report["review_status"] = report["reviews"][-1]["status"] if report["reviews"] else "pending"
    json_path, md_path = out / f"{run_id}.json", out / f"{run_id}.md"
    atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(md_path, markdown(report))
    return {"json": str(json_path), "markdown": str(md_path)}
