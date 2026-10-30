"""独立项目报告：文字均转义，可通过浏览器保存为 PDF。"""

from html import escape


def _text(value) -> str:
    return escape(str(value if value is not None else "未填写"))


def _assessment_html(assessment: dict | None) -> str:
    if not assessment:
        return "<p>尚未生成逐条要求与证据对照表。</p>"
    labels = {
        "CANDIDATE_EVIDENCE": "候选正文，待核查",
        "CATALOG_ONLY": "只有目录，缺少正文",
        "NO_EVIDENCE": "未找到依据",
    }
    html = f"<p>证据快照时间：{_text(assessment['created_at'])}</p>"
    html += "<p class=muted>这部分保留当次资料和证据；后续资料变化请重新检索。候选条文不代表合规结论。</p>"
    decisions = {decision["item_index"]: decision for decision in assessment.get("decisions", [])}
    decision_labels = {
        "CONFIRMED": "已确认候选依据",
        "NEEDS_CHECK": "待进一步核查",
        "NOT_APPLICABLE": "本项目不适用",
    }
    for item in assessment["items"]:
        requirement = item["requirement"]
        html += f"<article><h3>{_text(requirement['text'])}</h3><p>要求出处：{_text(requirement['source_name'])}</p><p>状态：{_text(labels.get(item['status'], '待核查'))}</p>"
        for evidence in item["search"]["results"]:
            html += f"<blockquote>{_text(evidence['text'])}<footer>{_text(evidence.get('standard_code') or evidence['regulation_name'])} · 第 {_text(evidence['clause_number'])} 条 · 页码 {_text(evidence.get('page_start'))} · {_text(evidence.get('source_url'))}</footer></blockquote>"
            if evidence.get("source_warning"):
                html += f"<p>{_text(evidence['source_warning'])}</p>"
        for catalog in item["search"]["catalog_suggestions"]:
            html += f"<p>目录线索：{_text(catalog['standard_code'])} {_text(catalog['title'])} · {_text(catalog['reason'])} · {_text(catalog.get('official_url'))}</p>"
        decision = decisions.get(item.get("index"))
        if decision:
            html += f"<p><strong>人工处理：{_text(decision_labels[decision['action']])}</strong></p><p>复核意见：{_text(decision['comment'])}</p><p>保存时间：{_text(decision['updated_at'])}</p>"
            selected = decision.get("selected_evidence")
            if selected:
                html += f"<p>确认条文：{_text(selected.get('standard_code') or selected.get('regulation_name'))} · 第 {_text(selected['clause_number'])} 条</p>"
        else:
            html += "<p>人工处理：尚未确认</p>"
        html += "</article>"
    return html


def render_dossier_report(dossier: dict, detail) -> str:
    project = dossier["project"]
    # 缺少许可和没有要求是两回事，隐藏内容时必须说明原因，但不带出受限原文。
    notices = "".join(
        f'<p class="muted">资料使用限制：{_text(item["notice"])}</p>'
        for item in dossier.get("content_notices", [])
    )
    conditions = (
        "".join(
            f"<li><strong>{_text(item['label'])}：{_text(item['value'])} {_text(item['unit'])}</strong>"
            f"<p>原文：{_text(item['source_text'])} · 来源：{_text(item['source_name'])} · 待确认</p></li>"
            for item in dossier["conditions"]
        )
        or "<li>没有识别到明确的数值条件。</li>"
    )
    requirements = (
        "".join(
            f'<li>{_text(item["text"])}<p class="muted">来源：{_text(item["source_name"])}</p></li>'
            for item in dossier["requirements"]
        )
        or "<li>尚未从项目资料中提取到要求。</li>"
    )
    topics = (
        "".join(
            f"<li><strong>{_text(item['label'])}</strong>：{_text(item['reason'])}</li>"
            for item in dossier["topics"]
        )
        or "<li>暂无专题线索。</li>"
    )
    analysis = "<p>尚未运行 AI 分析。</p>"
    if detail is not None:
        result = detail.run.final_result
        if result:
            decisions = {item.finding_index: item.decision.value for item in detail.decisions}
            labels = {"ACCEPTED": "采纳", "REJECTED": "驳回", "NEEDS_REVIEW": "待进一步复核"}
            analysis = f"<p>{_text(result.get('summary'))}</p>"
            analysis += "<p class=muted>人工处理完成表示已记录判断，不表示整个项目合规。</p>"
            for index, finding in enumerate(result.get("findings", [])):
                analysis += f"<article><h3>{_text(finding['title'])}</h3><p>{_text(finding['statement'])}</p>"
                statuses = {
                    "SUPPORTED": "有规范依据",
                    "RISK": "发现风险",
                    "INSUFFICIENT_EVIDENCE": "证据不足",
                    "NEEDS_DRAWING": "需要结合图纸",
                }
                analysis += (
                    f"<p>分析状态：{_text(statuses.get(finding.get('status'), '待核查'))}</p>"
                )
                if finding.get("guard_message"):
                    analysis += f"<p>{_text(finding['guard_message'])}</p>"
                analysis += f"<p>建议：{_text(finding['suggested_action'])}</p>"
                for citation in finding.get("citations", []):
                    analysis += f"<blockquote>{_text(citation['text'])}<footer>{_text(citation.get('standard_code'))} · 第 {_text(citation['clause_number'])} 条 · {_text(citation.get('source_url'))}</footer></blockquote>"
                analysis += (
                    f"<p>人工判断：{labels.get(decisions.get(index), '尚未确认')}</p></article>"
                )
            for limitation in result.get("limitations", []):
                analysis += f'<p class="muted">使用边界：{_text(limitation)}</p>'
        else:
            analysis = f"<p>最新分析尚无完整结果：{_text(detail.run.error_message or detail.run.status.value)}</p>"
    missing = "、".join(dossier["missing_fields"]) or "已填写主要数值条件"
    assessment = _assessment_html(dossier.get("assessment"))
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>{_text(project["name"])} · 项目分析报告</title>
<style>body{{font-family:"Microsoft YaHei",sans-serif;color:#243a34;background:#f4f4ef;line-height:1.8}}main{{max-width:900px;margin:40px auto;background:white;padding:40px}}h1,h2{{color:#164d40}}h2{{border-bottom:1px solid #dce6df;padding-bottom:10px;margin-top:30px}}li{{margin:12px 0}}blockquote,article{{background:#f4f7f4;padding:18px;border-left:3px solid #34715e}}footer,.muted{{color:#687972;font-size:13px}}button{{padding:12px 20px;background:#164d40;color:white;border:0;border-radius:8px}}@media print{{body{{background:white}}main{{margin:0;padding:0}}.toolbar{{display:none}}article{{break-inside:avoid}}}}</style>
<main><div class="toolbar"><button onclick="window.print()">打印 / 保存 PDF</button></div>
<p>ARCHSPEC · 项目分析报告</p><h1>{_text(project["name"])}</h1>
<p>地区：{_text(project.get("city"))} · 用途：{_text(project.get("building_type_label"))} · 阶段：{_text(project.get("design_stage_label"))}</p>
<p class="muted">本报告整理项目资料、核查线索和 AI 草稿；最终依据须核对现行正式规范及图纸。</p>{notices}
<h2>一、资料识别的条件候选</h2><ul>{conditions}</ul><p>档案待补充：{_text(missing)}</p>
<h2>二、任务书与设计要求</h2><ol>{requirements}</ol>
<h2>三、专题核查清单</h2><ul>{topics}</ul>
<h2>四、逐条要求与规范证据对照</h2>{assessment}
<h2>五、最新 AI 分析与人工判断</h2>{analysis}</main></html>"""
