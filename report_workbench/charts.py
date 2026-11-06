"""Deterministic, escaped SVG from persisted numeric evidence; no model markup."""
from __future__ import annotations

import html
from decimal import Decimal


def render_charts(evidence: list[dict]) -> str:
    figures = []
    for item in evidence:
        for chart in item.get('analysis', {}).get('charts', []):
            points = chart['points']
            values = [Decimal(p['value']) for p in points if p['value'] is not None]
            if not values:
                continue
            low, high = min(min(values), Decimal(0)), max(max(values), Decimal(0))
            span = high - low or Decimal(1)
            y = lambda value: 190 - float((value - low) / span) * 145
            x = lambda index: 70 + index * (620 / max(1, len(points) - 1))
            baseline = y(Decimal(0))
            title = html.escape(f"{chart['source']} / {chart['column']}")
            for kind, label in [('bar', '柱状图'), ('line', '行序折线图')]:
                shapes = [f'<line x1="55" y1="{baseline:.2f}" x2="710" y2="{baseline:.2f}" stroke="#93aaa2"/>']
                previous = None
                width = min(24, 440 / max(1, len(points)))
                for index, point in enumerate(points):
                    if point['value'] is None:
                        previous = None
                        continue
                    px, py = x(index), y(Decimal(point['value']))
                    tip = html.escape(f"行 {point['row']}：{point['value']}")
                    if kind == 'bar':
                        shapes.append(f'<rect x="{px-width/2:.2f}" y="{min(py,baseline):.2f}" width="{width:.2f}" height="{max(abs(py-baseline),1):.2f}" fill="#207b6b"><title>{tip}</title></rect>')
                    else:
                        if previous is not None:
                            shapes.append(f'<line x1="{previous[0]:.2f}" y1="{previous[1]:.2f}" x2="{px:.2f}" y2="{py:.2f}" stroke="#207b6b" stroke-width="2"/>')
                        shapes.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3" fill="#207b6b"><title>{tip}</title></circle>')
                        previous = (px, py)
                    if index % max(1, len(points)//8) == 0 or index == len(points)-1:
                        shapes.append(f'<text x="{px:.2f}" y="213" text-anchor="middle" font-size="11">{point["row"]}</text>')
                svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 235" role="img" aria-label="{title} {label}"><title>{title} {label}</title><text x="55" y="20" font-size="12">数值（单位按列名，未推断）</text><text x="8" y="50" font-size="10">{html.escape(str(high))}</text><text x="8" y="190" font-size="10">{html.escape(str(low))}</text>{"".join(shapes)}<text x="700" y="232" font-size="11">原始行序</text></svg>'
                figures.append(f'<figure class="data-chart"><h3>{title} · {label}</h3>{svg}<figcaption>{html.escape(chart["note"])} 实际展示 {len(points)} / {chart["rows"]} 条记录；精确值见报告的图表数据表。</figcaption></figure>')
    return '<section class="charts"><h2>可核对的 CSV 图表</h2>' + ''.join(figures) + '</section>' if figures else ''


def compare_reports(left: dict, right: dict) -> dict:
    """Match unique name/kind pairs; compare numeric facts, never model prose."""
    def index(job):
        groups = {}
        for item in job.get('evidence', []):
            key = (item['source']['name'], item['source']['kind'])
            groups.setdefault(key, []).append(item)
        return groups
    a, b = index(left), index(right)
    sources, metrics = [], []
    for name, kind in sorted(a.keys() | b.keys()):
        la, rb = a.get((name, kind), []), b.get((name, kind), [])
        status = 'ambiguous' if len(la) > 1 or len(rb) > 1 else 'added' if not la else 'removed' if not rb else 'same' if la[0]['source']['sha256'] == rb[0]['source']['sha256'] else 'changed'
        sources.append({'name': name, 'kind': kind, 'status': status,
                        'left_sha256': la[0]['source']['sha256'] if len(la) == 1 else None,
                        'right_sha256': rb[0]['source']['sha256'] if len(rb) == 1 else None})
        if len(la) != 1 or len(rb) != 1:
            continue
        aa, bb = la[0]['analysis'], rb[0]['analysis']
        def add(column, metric, av, bv):
            metrics.append({'source': name, 'column': column, 'metric': metric,
                            'left': str(av), 'right': str(bv), 'delta': str(Decimal(str(bv))-Decimal(str(av)))})
        for metric in ('rows', 'characters', 'lines'):
            if metric in aa and metric in bb:
                add('', metric, aa[metric], bb[metric])
        ac = {c['name']: c for c in aa.get('columns', []) if c['type'] == 'numeric'}
        bc = {c['name']: c for c in bb.get('columns', []) if c['type'] == 'numeric'}
        for column in sorted(ac.keys() & bc.keys()):
            for metric in ('total', 'mean', 'median', 'minimum', 'maximum', 'nonempty', 'missing'):
                if metric in ac[column] and metric in bc[column]:
                    add(column, metric, ac[column][metric], bc[column][metric])
    return {'left': {'id': left['id'], 'title': left['title']}, 'right': {'id': right['id'], 'title': right['title']},
            'sources': sources, 'metrics': metrics,
            'note': '只比较持久化的确定性指标，差值为右侧减左侧。按唯一文件名与类型匹配；重复名称不配对。相同列名不保证单位、时间范围或样本可比；不比较模型文字。'}
