"""导出互动记录为 Excel（按方向分 sheet）、CSV 与 HTML 日志（方向分两区 × 转赞评分块）。

Excel / CSV 直接用 openpyxl + 标准库 csv 写出，不再依赖 pandas：
pandas 在本工程里只用于导出，而 Excel 引擎本来就是 openpyxl，
移除 pandas 后打包体积少约 19 MB（pandas 13 MB + numpy 6 MB），冷启动也更快。
"""

from __future__ import annotations

import csv
import html as html_mod
import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

import config
from models import FailedWeibo, InteractionRecord, UserInfo

COLUMNS: list[str] = [
    "时间",
    "互动类型",
    "方向",
    "发起方",
    "微博作者",
    "微博内容",
    "互动内容",
    "被回复人",
    "被回复评论",
    "微博时间",
    "微博链接",
    "互动链接",
]

# 类型 → 徽章颜色
TYPE_COLORS: dict[str, str] = {
    "转发": "#3b82f6",
    "评论": "#10b981",
    "评论回复": "#06b6d4",
    "@提及": "#8b5cf6",
    "点赞": "#f43f5e",
}

# 分区内展示顺序：转发 → 点赞 → 评论 → 评论回复（@提及并入评论区）
BLOCK_ORDER: dict[str, int] = {"转发": 0, "点赞": 1, "评论": 2, "评论回复": 3, "@提及": 2}
BLOCK_LABEL: dict[str, str] = {
    "转发": "转发",
    "点赞": "点赞",
    "评论": "评论",
    "评论回复": "评论回复",
    "@提及": "评论",
}
BLOCKS: list[str] = ["转发", "点赞", "评论", "评论回复"]

# 「刷新数据」按钮已按用户要求移除（HTML 不再渲染；重新抓取请用程序内「开始抓取」或命令行 main.py）

# 页面交互 JS（普通字符串，非 f-string，花括号无需转义）
MAIN_JS = r"""
var TOTAL_CARDS = document.querySelectorAll('.card').length;

function showZone(zone) {
  document.querySelectorAll('.ztab').forEach(b => b.classList.remove('active'));
  document.getElementById('ztab-' + zone).classList.add('active');
  document.querySelectorAll('.zone-panel').forEach(p => p.classList.remove('active'));
  var panel = document.getElementById(zone);
  panel.classList.add('active');
  var first = panel.querySelector('.stab');
  if (first) showBlock(zone, first.getAttribute('data-block'));
}

function showBlock(zone, block) {
  document.querySelectorAll('#' + zone + ' .stab').forEach(b => b.classList.remove('active'));
  document.querySelector('#' + zone + ' .stab[data-block="' + block + '"]').classList.add('active');
  document.querySelectorAll('#' + zone + ' .sub-panel').forEach(p => p.classList.remove('active'));
  document.getElementById(zone + '-' + block).classList.add('active');
}

function visibleRows() {
  var rows = [];
  document.querySelectorAll('.card').forEach(function (c) {
    if (c.style.display === 'none') return;
    rows.push({
      t: c.getAttribute('data-type'),
      d: (c.getAttribute('data-day') || '').slice(0, 7),
      day: c.getAttribute('data-day') || '',
      dir: c.getAttribute('data-dir') || ''
    });
  });
  return rows;
}

function refreshStats() {
  var rows = visibleRows();
  var total = rows.length;
  var byType = { '转发': 0, '评论': 0, '评论回复': 0, '点赞': 0 };
  var byDir = { 'A→B': 0, 'B→A': 0 };
  var byZoneBlock = {};
  rows.forEach(function (r) {
    if (byType[r.t] !== undefined) byType[r.t]++;
    if (byDir[r.dir] !== undefined) byDir[r.dir]++;
    var zone = r.dir === 'A→B' ? 'toB' : 'toA';
    var block = (r.t === '@提及') ? '评论' : r.t;
    var key = zone + '|' + block;
    byZoneBlock[key] = (byZoneBlock[key] || 0) + 1;
  });
  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-转发').textContent = byType['转发'];
  document.getElementById('stat-评论').textContent = byType['评论'];
  document.getElementById('stat-评论回复').textContent = byType['评论回复'];
  document.getElementById('stat-点赞').textContent = byType['点赞'];
  document.getElementById('stat-toB').textContent = byDir['A→B'];
  document.getElementById('stat-toA').textContent = byDir['B→A'];
  var tl = document.getElementById('stat-total-line');
  if (tl) tl.textContent = total;
  document.querySelectorAll('.ztab').forEach(function (b) {
    var zone = b.id.replace('ztab-', '');
    var dir = zone === 'toB' ? 'A→B' : 'B→A';
    var span = b.querySelector('.n');
    if (span) span.textContent = byDir[dir] || 0;
  });
  document.querySelectorAll('.stab').forEach(function (b) {
    var zone = b.closest('.zone-panel').id;
    var block = b.getAttribute('data-block');
    var span = b.querySelector('.n');
    if (span) span.textContent = byZoneBlock[zone + '|' + block] || 0;
  });
  document.querySelectorAll('.zone-panel').forEach(function (p) {
    var dir = p.id === 'toB' ? 'A→B' : 'B→A';
    var cnt = p.querySelector('.zone-count');
    if (cnt) cnt.textContent = '共 ' + (byDir[dir] || 0) + ' 条';
  });
}

var TREND_COLORS = { '评论': '#10b981', '转发': '#3b82f6', '点赞': '#f43f5e' };

function buildTrend() {
  var rows = visibleRows();
  var months = {}, order = [];
  rows.forEach(function (r) {
    if (!r.d) return;
    if (!months[r.d]) { months[r.d] = { '评论': 0, '转发': 0, '点赞': 0 }; order.push(r.d); }
    if (r.t === '评论回复') months[r.d]['评论']++;
    else if (months[r.d][r.t] !== undefined) months[r.d][r.t]++;
  });
  order.sort();
  var box = document.getElementById('trend-box');
  if (order.length === 0) {
    box.innerHTML = '<div class="trend-empty">当前筛选范围内暂无数据</div>';
    return;
  }
  var W = 820, H = 210, L = 46, R = 18, T = 28, B = 36;
  var iw = W - L - R, ih = H - T - B;
  var maxV = 1;
  order.forEach(function (m) {
    var d = months[m];
    maxV = Math.max(maxV, d['评论'], d['转发'], d['点赞']);
  });
  function x(i) { return order.length === 1 ? L + iw / 2 : L + i * iw / (order.length - 1); }
  function y(v) { return T + ih - (v / maxV) * ih; }
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="trend-svg" preserveAspectRatio="xMidYMid meet">';
  for (var g = 0; g <= 4; g++) {
    var gy = T + ih * g / 4;
    var gv = Math.round(maxV * (4 - g) / 4);
    s += '<line x1="' + L + '" y1="' + gy + '" x2="' + (W - R) + '" y2="' + gy + '" stroke="#e8edf3" stroke-width="1"/>';
    s += '<text x="' + (L - 6) + '" y="' + (gy + 4) + '" text-anchor="end" class="trend-axis">' + gv + '</text>';
  }
  order.forEach(function (m, i) {
    s += '<text x="' + x(i) + '" y="' + (H - B + 16) + '" text-anchor="middle" class="trend-x">' + m + '</text>';
  });
  ['评论', '转发', '点赞'].forEach(function (k) {
    var pts = order.map(function (m, i) { return x(i) + ',' + y(months[m][k]); }).join(' ');
    s += '<polyline points="' + pts + '" fill="none" stroke="' + TREND_COLORS[k] + '" stroke-width="2"/>';
    order.forEach(function (m, i) {
      var cx = x(i), cy = y(months[m][k]);
      s += '<circle cx="' + cx + '" cy="' + cy + '" r="3.5" fill="#fff" stroke="' + TREND_COLORS[k] + '" stroke-width="2"/>';
      s += '<text x="' + cx + '" y="' + (cy - 7) + '" text-anchor="middle" class="trend-num" fill="' + TREND_COLORS[k] + '">' + months[m][k] + '</text>';
    });
  });
  s += '</svg>';
  box.innerHTML = s;
}

function applyFilter() {
  var kw = (document.getElementById('kw').value || '').trim().toLowerCase();
  var ty = document.getElementById('ftype').value;
  var d0 = document.getElementById('fstart').value;
  var d1 = document.getElementById('fend').value;
  var shown = 0;
  document.querySelectorAll('.card').forEach(function (c) {
    var ok = true;
    if (ty && c.getAttribute('data-type') !== ty) ok = false;
    if (ok && kw && (c.getAttribute('data-search') || '').indexOf(kw) === -1) ok = false;
    if (ok && d0 && (c.getAttribute('data-day') || '') < d0) ok = false;
    if (ok && d1 && (c.getAttribute('data-day') || '') > d1) ok = false;
    c.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  document.getElementById('fcnt').textContent = '显示 ' + shown + ' / ' + TOTAL_CARDS + ' 条';
  refreshStats();
  buildTrend();
}

function clearFilter() {
  document.getElementById('kw').value = '';
  document.getElementById('ftype').value = '';
  document.getElementById('fstart').value = '';
  document.getElementById('fend').value = '';
  applyFilter();
}

showZone('toB');
applyFilter();
"""


def _split_directions(
    records: list[InteractionRecord],
) -> tuple[list[InteractionRecord], list[InteractionRecord]]:
    """把记录按方向分成 (A→B, B→A) 两组。"""
    to_b = [r for r in records if r.get("方向") == "A→B"]
    to_a = [r for r in records if r.get("方向") == "B→A"]
    return to_b, to_a


def _sort_block(records: list[InteractionRecord]) -> list[InteractionRecord]:
    """块内排序：转发→点赞→评论(评论→评论回复→@提及)，同类型按时间倒序。"""
    type_order = {"转发": 0, "点赞": 1, "评论": 2, "评论回复": 3, "@提及": 4}
    return sorted(
        records, key=lambda r: (type_order.get(r["互动类型"], 9), str(r.get("时间") or "")), reverse=False
    )


# ---------- Excel / CSV 写出（不依赖 pandas） ----------


def _table_row(r: InteractionRecord) -> list[str]:
    """按 COLUMNS 顺序取一行，全部转成字符串（None → 空串）。

    说明：早期版本对 微博内容 / 互动内容 做过 120 字截断，但那份截断后的数据
    只用于汇总表（只按「互动类型 × 方向」分组，用不到正文），Excel 和 CSV
    实际写出的是未截断的完整记录 —— 也就是说截断是死代码。这里保持与真实
    行为一致：表格与 HTML 一样保留全文。
    """
    row: list[str] = []
    for c in COLUMNS:
        v = r.get(c)
        row.append("" if v is None else str(v))
    return row


def _write_sheet(ws: Worksheet, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """写入表头与数据，并按最长内容设置列宽（10~60），冻结首行。"""
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    for idx in range(1, len(header) + 1):
        if rows:
            longest = max(len(str(r[idx - 1])) for r in rows)
            width = max(10, min(60, longest + 2))
        else:
            width = 10
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"


def _build_pivot(records: list[InteractionRecord]) -> tuple[list[str], list[list[Any]]]:
    """汇总表：互动类型 × 方向 的计数，返回 (columns, rows)。

    替代原来的 pandas groupby + pivot，行为对齐：
    按类型名排序、缺失的方向补 0、末行「总计」为各列合计。
    """
    counts: dict[str, dict[str, int]] = {}
    for r in records:
        t = str(r.get("互动类型") or "")
        d = str(r.get("方向") or "")
        counts.setdefault(t, {})
        counts[t][d] = counts[t].get(d, 0) + 1

    # 方向列：固定 A→B / B→A 在前，若出现其它方向（理论上不会）追加在后
    dirs = ["A→B", "B→A"]
    for extra in sorted({d for t in counts for d in counts[t]} - set(dirs)):
        dirs.append(extra)

    columns = ["互动类型", *dirs, "合计"]
    rows: list[list[Any]] = []
    totals: dict[str, int] = dict.fromkeys(dirs, 0)
    for t in sorted(counts):
        row: list[Any] = [t]
        row_total = 0
        for d in dirs:
            n = counts[t].get(d, 0)
            totals[d] += n
            row_total += n
            row.append(n)
        row.append(row_total)
        rows.append(row)
    rows.append(["总计", *[totals[d] for d in dirs], sum(totals.values())])
    return columns, rows


def export(
    records: list[InteractionRecord],
    user_a: UserInfo,
    user_b: UserInfo,
    out_dir: str | None = None,
    formats: set[str] | None = None,
    failed_weibos: list[FailedWeibo] | None = None,
    enable_refresh: bool = False,
) -> dict[str, Any]:
    """导出互动记录，返回各文件路径（未勾选的格式不在返回中）。

    formats：{'html','excel','csv'} 子集，默认三者全出。
    failed_weibos：本次抓取最终失败的微博列表，用于 HTML 顶部提示。
    enable_refresh：HTML 是否渲染「刷新数据」按钮（依赖本地刷新服务，仅定制版用）。
    """
    formats = formats or {"html", "excel", "csv"}
    failed_weibos = failed_weibos or []
    out_dir = out_dir or config.OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    a_name, b_name = user_a["screen_name"], user_b["screen_name"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"微博互动_{a_name}_{b_name}_{ts}"
    result: dict[str, Any] = {"count": len(records), "pivot": None}

    if not records:
        if "excel" in formats:
            excel_path = os.path.join(out_dir, base + ".xlsx")
            wb = Workbook()
            del wb["Sheet"]
            _write_sheet(wb.create_sheet("互动记录"), COLUMNS, [])
            wb.save(excel_path)
            result["excel"] = excel_path
        if "csv" in formats:
            csv_path = os.path.join(out_dir, base + ".csv")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerow(COLUMNS)
            result["csv"] = csv_path
        if "html" in formats:
            html_path = os.path.join(out_dir, base + ".html")
            write_html_log(
                [],
                user_a,
                user_b,
                html_path,
                "无",
                failed_weibos=failed_weibos,
                enable_refresh=enable_refresh,
            )
            result["html"] = html_path
        return result

    # 汇总表：类型 × 方向（替代 pandas groupby + pivot）
    pivot_cols, pivot_rows = _build_pivot(records)
    result["pivot"] = {"columns": pivot_cols, "rows": pivot_rows}

    to_b, to_a = _split_directions(records)
    sorted_all = _sort_block(to_b) + _sort_block(to_a)

    # Excel：两个方向 sheet（通用命名 A→B / B→A）+ 汇总统计，块内排序
    if "excel" in formats:
        excel_path = os.path.join(out_dir, base + ".xlsx")
        wb = Workbook()
        del wb["Sheet"]  # 去掉 openpyxl 默认的空 sheet
        for sheet_name, grp in (("A→B", to_b), ("B→A", to_a)):
            rows = [_table_row(r) for r in _sort_block(grp)]
            _write_sheet(wb.create_sheet(sheet_name), COLUMNS, rows)
        _write_sheet(wb.create_sheet("汇总统计"), pivot_cols, pivot_rows)
        wb.save(excel_path)
        result["excel"] = excel_path

    if "csv" in formats:
        csv_path = os.path.join(out_dir, base + ".csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for r in sorted_all:
                w.writerow(_table_row(r))
        result["csv"] = csv_path

    if "html" in formats:
        html_path = os.path.join(out_dir, base + ".html")
        write_html_log(
            records,
            user_a,
            user_b,
            html_path,
            str(len(records)),
            failed_weibos=failed_weibos,
            enable_refresh=enable_refresh,
        )
        result["html"] = html_path

    return result


def _card_html(r: InteractionRecord) -> str:
    itype = r["互动类型"]
    color = TYPE_COLORS.get(itype, "#64748b")
    actor = html_mod.escape(str(r.get("发起方") or ""))
    author = html_mod.escape(str(r.get("微博作者") or ""))
    wb_text = html_mod.escape(str(r.get("微博内容") or ""))
    act_text = html_mod.escape(str(r.get("互动内容") or ""))
    t = html_mod.escape(str(r.get("时间") or ""))
    wb_ts = html_mod.escape(str(r.get("微博时间") or ""))
    wb_url = html_mod.escape(str(r.get("微博链接") or ""))
    act_url = html_mod.escape(str(r.get("互动链接") or ""))

    act_content = (
        f'<div class="act">{act_text}</div>' if act_text else '<div class="act muted">（无附加内容）</div>'
    )
    approval_tag = (
        '<span class="badge" style="background:#f59e0b">待审核</span>'
        if act_text.startswith("【待审核】")
        else ""
    )
    link_part = ""
    if act_url:
        link_part = f' · <a href="{act_url}" target="_blank">互动者主页</a>'
    reply_part = ""
    reply_search = ""
    if itype == "评论回复":
        rto_user = html_mod.escape(str(r.get("被回复人") or ""))
        rto_text = html_mod.escape(str(r.get("被回复评论") or ""))
        reply_search = rto_user + " " + rto_text
        if rto_text:
            who = f"<b>{rto_user}</b>" if rto_user else "对方"
            reply_part = (
                f'<div class="replyto">↪ 回复 {who} 的评论：<span class="rtext">{rto_text}</span></div>'
            )
    day = t[:10]
    direction = html_mod.escape(str(r.get("方向") or ""))
    data_search = html_mod.escape(" ".join([itype, actor, author, wb_text, act_text, reply_search]).lower())
    return f"""
        <div class="card" data-type="{itype}" data-day="{day}" data-dir="{direction}" data-search="{data_search}">
          <div class="meta">
            <span class="badge" style="background:{color}">{itype}</span>
            {approval_tag}
            <span class="time">{t}</span>
          </div>
          <div class="line"><b>{actor}</b> 互动了 <b>{author}</b> 的微博{link_part}</div>
          {act_content}
          {reply_part}
          <div class="quote">
            <div class="quote-head">原微博 · {wb_ts}</div>
            <div class="quote-body">{wb_text}</div>
            <a href="{wb_url}" target="_blank">查看原微博 →</a>
          </div>
        </div>"""


def _block_panel_html(zone_id: str, block: str, records: list[InteractionRecord]) -> str:
    panel_id = f"{zone_id}-{block}"
    if not records:
        return f'<div id="{panel_id}" class="sub-panel"><div class="empty-mini">该分类下暂无记录</div></div>'
    cards = "".join(_card_html(r) for r in records)
    return f'<div id="{panel_id}" class="sub-panel">{cards}</div>'


def _zone_html(zone_id: str, title: str, count: int, records: list[InteractionRecord]) -> str:
    blocks: dict[str, list[InteractionRecord]] = {b: [] for b in BLOCKS}
    for r in records:
        label = BLOCK_LABEL.get(r["互动类型"], "评论")
        blocks.setdefault(label, []).append(r)

    # 大页头部 + 小选项卡按钮
    tab_btns = ""
    panels = ""
    for label in BLOCKS:
        n = len(blocks.get(label, []))
        tab_btns += (
            f'<button class="stab" data-block="{label}" '
            f"onclick=\"showBlock('{zone_id}', '{label}')\">"
            f'{label}<span class="n">{n}</span></button>'
        )
        panels += _block_panel_html(zone_id, label, _sort_block(blocks.get(label, [])))
    tab_btns = f'<div class="stab-bar">{tab_btns}</div>'

    return f"""
    <section class="zone-panel" id="{zone_id}">
      <div class="zone-head">
        <h2>{title}</h2>
        <span class="zone-count">共 {count} 条</span>
      </div>
      {tab_btns}
      {panels}
    </section>"""


def _stats_html(a_name: str, b_name: str) -> str:
    """顶部统计卡片（带 id，过滤时由 JS 联动更新）+ 折线图容器。

    折线图由 JS 从当前可见卡片构建（三类三色：评论=评论+评论回复合并、转发、点赞），
    过滤后自动重绘。
    """

    def card(cid: str, label: str, num: int, color: str) -> str:
        return (
            f'<div class="stat-card"><div class="stat-num" id="{cid}" style="color:{color}">{num}</div>'
            f'<div class="stat-label">{label}</div></div>'
        )

    cards = card("stat-total", "总互动", 0, "#0f172a")
    cards += card("stat-转发", "转发", 0, "#3b82f6")
    cards += card("stat-评论", "评论", 0, "#10b981")
    cards += card("stat-评论回复", "评论回复", 0, "#06b6d4")
    cards += card("stat-点赞", "点赞", 0, "#f43f5e")
    cards += card("stat-toB", f"{a_name} → {b_name}", 0, "#7c3aed")
    cards += card("stat-toA", f"{b_name} → {a_name}", 0, "#ea580c")

    legend = (
        '<span class="trend-legend">'
        '<span><i style="background:#10b981"></i>评论（含评论回复）</span>'
        '<span><i style="background:#3b82f6"></i>转发</span>'
        '<span><i style="background:#f43f5e"></i>点赞</span>'
        "</span>"
    )
    return (
        f'<div class="stats-row">{cards}</div>'
        f'<div class="trend-wrap">'
        f'<div class="trend-head"><span class="trend-title">每月互动趋势</span>{legend}</div>'
        f'<div id="trend-box"></div>'
        f"</div>"
    )


def write_html_log(
    records: list[InteractionRecord],
    user_a: UserInfo,
    user_b: UserInfo,
    path: str,
    count_label: str,
    failed_weibos: list[FailedWeibo] | None = None,
    enable_refresh: bool = False,
) -> None:
    """把互动记录渲染成选项卡式 HTML：
    大标签页 = 方向（A→B / B→A）；大页内小标签页 = 转发 / 点赞 / 评论 / 评论回复。
    顶部含统计卡片 + 搜索/类型/日期过滤；failed_weibos 非空时显示失败提示条。
    （刷新按钮已按用户要求移除；enable_refresh 参数保留仅为兼容旧调用。）
    """
    failed_weibos = failed_weibos or []
    a_name = html_mod.escape(user_a["screen_name"] or "用户A")
    b_name = html_mod.escape(user_b["screen_name"] or "用户B")

    if not records:
        body_html = '<div class="empty">近时间范围内未发现互动记录。</div>'
        zone_tabs = ""
        stats_html = _stats_html(a_name, b_name)
    else:
        to_b, to_a = _split_directions(records)
        zone_tabs = (
            f'<button class="ztab active" id="ztab-toB" onclick="showZone(\'toB\')">'
            f'<span class="ztitle">{a_name} → {b_name}</span>'
            f'<span class="n">{len(to_b)}</span></button>'
            f'<button class="ztab" id="ztab-toA" onclick="showZone(\'toA\')">'
            f'<span class="ztitle">{b_name} → {a_name}</span>'
            f'<span class="n">{len(to_a)}</span></button>'
        )
        body_html = _zone_html("toB", f"{a_name} → {b_name}", len(to_b), to_b) + _zone_html(
            "toA", f"{b_name} → {a_name}", len(to_a), to_a
        )
        stats_html = _stats_html(a_name, b_name)

    fail_bar = ""
    if failed_weibos:
        fail_bar = (
            f'<div class="fail-bar">⚠ 有 <b>{len(failed_weibos)}</b> 条微博抓取失败'
            f"（重试后仍失败），其互动可能缺失"
            f'<span class="fail-detail">{
                html_mod.escape(
                    "；".join(
                        f"{f.get('stage')} {f.get('mid')} {f.get('reason')}" for f in failed_weibos[:10]
                    )
                )
            }</span></div>'
        )

    refresh_html = ""
    refresh_js = ""

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>微博互动日志：{a_name} × {b_name}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #f1f5f9; color: #1e293b; padding: 24px 16px; }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  header {{ background: #fff; border-radius: 14px; padding: 22px 26px; margin-bottom: 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; margin-bottom: 6px; }}
  .sub {{ color: #64748b; font-size: 13px; }}
  .stat {{ margin-top: 10px; font-size: 13px; color: #475569; }}
  .stat b {{ color: #0f172a; }}
  .fail-bar {{ background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; border-radius: 10px;
               padding: 10px 14px; font-size: 13px; margin-top: 12px; }}
  .fail-detail {{ display: block; color: #7f1d1d; font-size: 12px; margin-top: 4px;
                  word-break: break-all; }}
  .stats-row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
  .stat-card {{ flex: 1; min-width: 92px; background: #f8fafc; border: 1px solid #e8edf3;
                border-radius: 10px; padding: 10px 12px; text-align: center; }}
  .stat-num {{ font-size: 20px; font-weight: 700; }}
  .stat-label {{ font-size: 12px; color: #64748b; margin-top: 2px; }}
  .trend-wrap {{ margin-top: 14px; background: #f8fafc; border: 1px solid #e8edf3;
                 border-radius: 10px; padding: 12px 14px; }}
  .trend-head {{ display: flex; align-items: center; gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }}
  .trend-title {{ font-size: 13px; color: #475569; }}
  .trend-legend {{ display: inline-flex; gap: 14px; font-size: 12px; color: #64748b; flex-wrap: wrap; }}
  .trend-legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
  .trend-legend i {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
  .trend-svg {{ width: 100%; height: auto; display: block; }}
  .trend-axis {{ font-size: 10px; fill: #94a3b8; }}
  .trend-x {{ font-size: 10px; fill: #94a3b8; }}
  .trend-num {{ font-size: 10px; font-weight: 600; }}
  .trend-empty {{ color: #cbd5e1; font-size: 13px; text-align: center; padding: 30px 0; }}
  .filter-bar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
                 background: #fff; border-radius: 12px; padding: 12px 16px; margin-bottom: 14px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .filter-bar input[type=text], .filter-bar input[type=date], .filter-bar select {{
    border: 1px solid #cbd5e1; border-radius: 8px; padding: 6px 10px; font-size: 13px;
    font-family: inherit; background: #fff; }}
  .filter-bar input[type=text] {{ width: 200px; }}
  .filter-cnt {{ margin-left: auto; font-size: 12px; color: #64748b; }}
  .filter-clear {{ border: 1px solid #cbd5e1; background: #fff; border-radius: 8px; padding: 6px 12px;
                   font-size: 13px; cursor: pointer; color: #475569; }}
  .filter-clear:hover {{ background: #f1f5f9; }}
  .ztab-bar {{ display: flex; gap: 10px; margin-bottom: 0; }}
  .ztab {{ flex: 1; border: none; background: #e2e8f0; color: #475569; font-size: 15px; font-weight: 600;
           padding: 12px 14px; border-radius: 10px 10px 0 0; cursor: pointer;
           display: flex; align-items: center; justify-content: center; gap: 8px;
           border-bottom: 3px solid transparent; }}
  .ztab:hover {{ background: #cbd5e1; }}
  .ztab.active {{ background: #0f172a; color: #fff; border-bottom-color: #0f172a; }}
  .ztab .zsub {{ font-size: 12px; font-weight: 400; opacity: .72; min-width: 0;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .ztab .n, .stab .n {{ background: rgba(0,0,0,.08); color: inherit; font-size: 12px; font-weight: 600;
                         padding: 1px 8px; border-radius: 999px; flex-shrink: 0; }}
  .ztab.active .n {{ background: rgba(255,255,255,.22); color: #fff; }}
  .zone-panel {{ display: none; background: #fff; border-radius: 0 0 14px 14px;
                 padding: 18px 22px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
                 border-top: 1px solid #eef2f7; }}
  .zone-panel.active {{ display: block; }}
  .zone-head {{ display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }}
  .zone-head h2 {{ font-size: 17px; color: #0f172a; }}
  .zone-count {{ color: #64748b; font-size: 13px; }}
  .stab-bar {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;
               padding: 10px 12px; background: #f1f5f9; border-radius: 10px;
               border: 1px solid #e8edf3; }}
  .stab {{ border: none; background: transparent; color: #475569; font-size: 13px; font-weight: 600;
           padding: 7px 14px; border-radius: 8px; cursor: pointer;
           display: inline-flex; align-items: center; gap: 6px; }}
  .stab:hover {{ background: #e2e8f0; }}
  .stab.active {{ background: #2563eb; color: #fff; }}
  .stab.active .n {{ background: rgba(255,255,255,.25); color: #fff; }}
  .sub-panel {{ display: none; }}
  .sub-panel.active {{ display: block; }}
  .card {{ background: #fff; border: 1px solid #eef2f7; border-radius: 12px; padding: 14px 18px;
           margin-bottom: 12px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
  .meta {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }}
  .badge {{ color: #fff; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; }}
  .time {{ color: #94a3b8; font-size: 12px; margin-left: auto; }}
  .line {{ font-size: 14px; margin-bottom: 8px; }}
  .act {{ background: #f8fafc; border-radius: 8px; padding: 8px 12px; font-size: 14px;
          margin-bottom: 10px; word-break: break-word; }}
  .act.muted {{ color: #94a3b8; font-style: italic; }}
  .replyto {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 7px 12px;
              font-size: 13px; color: #1e40af; margin-bottom: 10px; word-break: break-word; }}
  .replyto .rtext {{ color: #1d4ed8; }}
  .quote {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 10px 12px; font-size: 13px; }}
  .quote-head {{ color: #b45309; font-size: 12px; margin-bottom: 4px; }}
  .quote-body {{ color: #78350f; word-break: break-word; margin-bottom: 4px; }}
  .quote a {{ color: #d97706; text-decoration: none; font-size: 12px; }}
  .empty {{ background: #fff; border-radius: 12px; padding: 40px; text-align: center; color: #94a3b8; }}
  .empty-mini {{ color: #cbd5e1; font-size: 13px; padding: 12px 0; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>微博互动日志：{a_name} × {b_name}</h1>
    <div class="sub">大标签页按方向分（A→B / B→A），页内按 转发 / 点赞 / 评论 / 评论回复 分小标签页</div>
    {fail_bar}
    {stats_html}
    <div class="stat">共 <b id="stat-total-line">{count_label}</b> 条互动记录{refresh_html}</div>
  </header>
  <div class="filter-bar">
    <input type="text" id="kw" placeholder="搜索关键词（内容/昵称/类型）…" oninput="applyFilter()">
    <select id="ftype" onchange="applyFilter()">
      <option value="">全部类型</option>
      <option>转发</option>
      <option>评论</option>
      <option>评论回复</option>
      <option>点赞</option>
    </select>
    <input type="date" id="fstart" onchange="applyFilter()">
    <span style="color:#94a3b8">~</span>
    <input type="date" id="fend" onchange="applyFilter()">
    <button class="filter-clear" onclick="clearFilter()">清空</button>
    <span class="filter-cnt" id="fcnt"></span>
  </div>
  <div class="ztab-bar">{zone_tabs}</div>
  {body_html}
</div>
<script>
{MAIN_JS}
{refresh_js}
</script>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
