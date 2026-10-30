"""把已经保存的审查结果渲染成可打印的中文报告。"""

from html import escape

from archspec.reviews.models import ReviewStepStatus
from archspec.reviews.service import ReviewReport


def _text(value: str | None, fallback: str = "—") -> str:
    """转义上传文档中的文字，避免它被浏览器当成网页代码执行。"""
    return escape(value or fallback)


def _evidence_html(item) -> str:
    evidence = item.confirmed_evidence
    if evidence is None:
        return (
            '<div class="pending-box">'
            '<strong>待人工核查</strong>'
            f"<p>{_text(item.conclusion)}</p>"
            "</div>"
        )

    identity = evidence.regulation_name
    if evidence.standard_code:
        identity = evidence.standard_code
        if evidence.standard_title:
            identity = f"{identity}《{evidence.standard_title}》"

    warning = evidence.source_warning or (
        "来源准入已通过；项目适用性仍待专业核对" if evidence.is_citable
        else "候选/历史关联，不代表规范核验通过"
    )
    warning_class = "verified" if evidence.is_citable else "warning"
    source_link = ""
    if evidence.source_url:
        safe_url = _text(evidence.source_url)
        source_link = (
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
            "查看来源页面</a>"
        )

    page = ""
    if evidence.page_start is not None:
        page = f" · 第 {evidence.page_start} 页"
        if evidence.page_end not in (None, evidence.page_start):
            page = f" · 第 {evidence.page_start}—{evidence.page_end} 页"

    return f"""
      <div class="evidence-box">
        <div class="evidence-title">{_text(identity)} · 第 {_text(evidence.clause_number)} 条{page}</div>
        <div class="source-badge {warning_class}">{_text(warning)}</div>
        <p class="heading">{_text(evidence.heading_path, '章节信息未标注')}</p>
        <blockquote>{_text(evidence.text)}</blockquote>
        {source_link}
      </div>
    """


def render_review_report(report: ReviewReport) -> str:
    """生成独立 HTML；浏览器可直接打印或另存为 PDF。"""
    confirmed = sum(
        item.status == ReviewStepStatus.CONFIRMED for item in report.items
    )
    pending = len(report.items) - confirmed
    finished = report.job.finished_at or report.job.started_at
    cards = []
    for index, item in enumerate(report.items, start=1):
        status_label = (
            "已核验来源，适用性待核对" if item.confirmed_evidence and item.confirmed_evidence.is_citable
            else "已确认候选关联" if item.confirmed_evidence else "待人工核查"
        )
        cards.append(
            f"""
            <article class="review-item">
              <div class="item-topline">
                <span>设计要求 {index}</span>
                <span class="status">{status_label}</span>
              </div>
              <p class="heading">{_text(item.heading_path, '任务书正文')}</p>
              <h2>{_text(item.requirement_text)}</h2>
              {_evidence_html(item)}
            </article>
            """
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>建筑规范审查报告</title>
  <style>
    :root {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; color: #172033; background: #f3f6fa; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 36px auto 64px; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 22px; }}
    .print-button {{ padding: 12px 20px; border: 0; border-radius: 10px; color: white; background: #2457d6; font: inherit; font-weight: 800; cursor: pointer; }}
    .report-header, .review-item {{ background: white; border: 1px solid #dbe2ec; border-radius: 16px; box-shadow: 0 8px 24px #1f304f0d; }}
    .report-header {{ padding: 30px; margin-bottom: 18px; }}
    h1 {{ margin: 0 0 8px; font-size: 32px; }}
    .muted, .heading {{ color: #647087; }}
    .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 22px; }}
    .metric {{ padding: 14px; border-radius: 11px; background: #f6f8fb; }}
    .metric strong {{ display: block; margin-top: 5px; font-size: 24px; }}
    .review-item {{ padding: 24px; margin-bottom: 14px; break-inside: avoid; }}
    .item-topline {{ display: flex; justify-content: space-between; gap: 12px; color: #2457d6; font-weight: 800; }}
    .status {{ color: #137a50; }}
    h2 {{ margin: 7px 0 18px; font-size: 20px; line-height: 1.6; }}
    .evidence-box, .pending-box {{ padding: 18px; border-radius: 12px; background: #f6f8fb; }}
    .pending-box {{ color: #7a5413; background: #fff7df; }}
    .evidence-title {{ color: #2457d6; font-weight: 800; }}
    .source-badge {{ display: inline-block; margin: 9px 0; padding: 4px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }}
    .source-badge.verified {{ color: #137a50; background: #e9f7f1; }}
    .source-badge.warning {{ color: #936313; background: #fff7df; }}
    blockquote {{ margin: 10px 0; padding-left: 14px; border-left: 3px solid #9bb6f7; line-height: 1.75; }}
    a {{ color: #2457d6; }}
    @media (max-width: 640px) {{ .toolbar {{ align-items: stretch; flex-direction: column; }} .metrics {{ grid-template-columns: 1fr; }} }}
    @media print {{ :root {{ background: white; }} main {{ width: 100%; margin: 0; }} .toolbar {{ display: none; }} .report-header, .review-item {{ box-shadow: none; }} }}
  </style>
</head>
<body>
  <main>
    <div class="toolbar">
      <span>审查记录 #{_text(report.job.id)}</span>
      <button class="print-button" type="button" onclick="window.print()">打印或保存为 PDF</button>
    </div>
    <header class="report-header">
      <h1>建筑规范审查报告</h1>
      <p class="muted">生成时间：{finished.astimezone().strftime('%Y-%m-%d %H:%M')}</p>
      <p>人工确认只记录设计要求与条文的关联；只有通过独立来源准入的正文可作为正式引用，项目适用性仍待专业核对。</p>
      <div class="metrics">
        <div class="metric">设计要求<strong>{len(report.items)}</strong></div>
        <div class="metric">已确认<strong>{confirmed}</strong></div>
        <div class="metric">待核查<strong>{pending}</strong></div>
      </div>
    </header>
    {''.join(cards)}
  </main>
</body>
</html>"""
