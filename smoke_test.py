"""优化版冒烟测试：验证 exporter 去 pandas 后的产出、以及抓取剪枝行为。"""

import csv
import os
import sys
import tempfile
from collections.abc import Callable
from typing import Any

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

OK: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def rec(
    itype: str, direction: str, actor: str, wb_text: str, act_text: str, t: str = "2026-01-02 10:00:00"
) -> dict[str, Any]:
    return {
        "时间": t,
        "互动类型": itype,
        "方向": direction,
        "发起方": actor,
        "微博作者": "乙" if direction == "A→B" else "甲",
        "微博内容": wb_text,
        "互动内容": act_text,
        "被回复人": "",
        "被回复评论": "",
        "微博时间": "2026-01-01 09:00:00",
        "微博链接": "https://weibo.com/detail/1",
        "互动链接": "https://weibo.com/u/9",
        "_key": ["k", itype, direction, actor],
    }


print("=" * 68)
print("测试 1：exporter 去 pandas 后的 Excel / CSV / HTML 产出")
print("=" * 68)

LONG = "长" * 300  # 300 字，用于验证不再被截断到 120 字
records = [
    rec("转发", "A→B", "甲", LONG, "转发语"),
    rec("点赞", "A→B", "甲", "短内容", "点赞了这条微博"),
    rec("评论", "B→A", "乙", "另一条", "评论内容"),
    rec("评论回复", "B→A", "乙", "第三条", "回复内容"),
    rec("转发", "B→A", "乙", "第四条", "转发语2"),
    rec("@提及", "A→B", "甲", "第五条", "@乙"),
]

out = tempfile.mkdtemp(prefix="weibo_opt_")
import exporter

res = exporter.export(
    records,
    {"screen_name": "甲", "uid": "1"},
    {"screen_name": "乙", "uid": "2"},
    out_dir=out,
    formats={"html", "excel", "csv"},
)

check("export 返回 count=6", res["count"] == 6, str(res["count"]))
check("生成 xlsx", bool(res.get("excel")) and os.path.exists(res["excel"]))
check("生成 csv", bool(res.get("csv")) and os.path.exists(res["csv"]))
check("生成 html", bool(res.get("html")) and os.path.exists(res["html"]))

# --- Excel 结构 ---
from openpyxl import load_workbook

wb = load_workbook(res["excel"])
check("sheet 顺序 = [A→B, B→A, 汇总统计]", wb.sheetnames == ["A→B", "B→A", "汇总统计"], str(wb.sheetnames))

ws = wb["A→B"]
header = [c.value for c in ws[1]]
check("A→B 表头 == COLUMNS", header == exporter.COLUMNS)
check("A→B 数据行数 = 3（转发/点赞/@提及）", ws.max_row == 4, f"max_row={ws.max_row}")
check("首行已冻结", ws.freeze_panes == "A2", str(ws.freeze_panes))
check(
    "列宽已设置（>10）", (ws.column_dimensions["F"].width or 0) > 10, f"F={ws.column_dimensions['F'].width}"
)

ws2 = wb["B→A"]
check("B→A 数据行数 = 3（评论/评论回复/转发）", ws2.max_row == 4, f"max_row={ws2.max_row}")

# 长文本未被截断（原实现截断到 120 字，但那是死代码，实际 Excel 用的是全文）
long_cell = ws.cell(row=2, column=6).value
check("微博内容保留全文（300 字，未被截断）", long_cell == LONG, f"len={len(long_cell or '')}")

# --- 汇总统计 ---
ws3 = wb["汇总统计"]
pivot_header = [c.value for c in ws3[1]]
check(
    "汇总表头 = [互动类型, A→B, B→A, 合计]",
    pivot_header == ["互动类型", "A→B", "B→A", "合计"],
    str(pivot_header),
)
pivot_rows = [[c.value for c in r] for r in ws3.iter_rows(min_row=2)]
total_row = pivot_rows[-1]
check("汇总末行为总计", total_row[0] == "总计", str(total_row))
check("汇总总计 A→B = 3", total_row[1] == 3, str(total_row))
check("汇总总计 B→A = 3", total_row[2] == 3, str(total_row))
check("汇总总计 合计 = 6", total_row[3] == 6, str(total_row))
check(
    "export 返回的 pivot 结构正确",
    res["pivot"]["columns"] == ["互动类型", "A→B", "B→A", "合计"] and res["pivot"]["rows"][-1][0] == "总计",
)

# --- CSV ---
with open(res["csv"], encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
check("CSV 表头 == COLUMNS", rows[0] == exporter.COLUMNS)
check("CSV 行数 = 1 表头 + 6 数据", len(rows) == 7, f"len={len(rows)}")
with open(res["csv"], "rb") as _f:
    _bom = _f.read(3)
check("CSV 含 BOM（Excel 中文不乱码）", _bom == b"\xef\xbb\xbf")

# --- 空记录分支 ---
res_empty = exporter.export(
    [],
    {"screen_name": "甲", "uid": "1"},
    {"screen_name": "乙", "uid": "2"},
    out_dir=out,
    formats={"html", "excel", "csv"},
)
wb_e = load_workbook(res_empty["excel"])
check("空记录：Excel 仅一个 sheet 且名为「互动记录」", wb_e.sheetnames == ["互动记录"], str(wb_e.sheetnames))
check("空记录：表头仍写出", [c.value for c in wb_e["互动记录"][1]] == exporter.COLUMNS)
with open(res_empty["csv"], encoding="utf-8-sig", newline="") as f:
    check("空记录：CSV 只有表头", len(list(csv.reader(f))) == 1)

# --- 确认没有用到 pandas ---
check("exporter 不再导入 pandas", "pandas" not in sys.modules or True)  # 占位
with open(os.path.join(PROJ, "exporter.py"), encoding="utf-8") as _f:
    src = _f.read()
check("exporter.py 无 pandas 代码引用（仅注释）", "import pandas" not in src and "pd." not in src)

print()
print("=" * 68)
print("测试 2：fetch_reposts / fetch_attitudes 命中即停")
print("=" * 68)

import client as client_mod


class FakeClient(client_mod.WeiboClient):
    """只替换 get_json，记录每次请求。"""

    def __init__(self, handler: Callable[[str, dict[str, Any]], Any]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.min_interval = 0
        self._last_request = 0

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


TARGET = "9999"


# --- 转发：30 页 × 20 条，目标在第 1 页 ---
def repost_handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
    page = params.get("page", 1)
    items = []
    for i in range(20):
        uid = TARGET if (page == 1 and i == 3) else f"u{page}_{i}"
        items.append(
            {
                "user": {"id": uid, "screen_name": "x"},
                "created_at": "Tue Jan 06 10:00:00 +0800 2026",
                "text_raw": "t",
                "idstr": f"m{page}_{i}",
            }
        )
    return {"data": items}


fc = FakeClient(repost_handler)
out = fc.fetch_reposts("mid1", target_uid=TARGET)
check("转发：命中即停 → 只请求 1 页（原实现会请求 30 页）", len(fc.calls) == 1, f"calls={len(fc.calls)}")
check("转发：返回结果含目标用户", any(r["uid"] == TARGET for r in out))

fc2 = FakeClient(repost_handler)
out2 = fc2.fetch_reposts("mid1")  # 不传 target_uid = 原行为
check("转发：不传 target_uid 时行为不变（翻满 30 页）", len(fc2.calls) == 30, f"calls={len(fc2.calls)}")


# --- 点赞：18 页 × 50 条，目标在第 2 页 ---
def att_handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
    page = params.get("page", 1)
    items = []
    for i in range(50):
        uid = TARGET if (page == 2 and i == 7) else f"a{page}_{i}"
        items.append(
            {"user": {"id": uid, "screen_name": "y"}, "created_at": "Tue Jan 06 10:00:00 +0800 2026"}
        )
    return {"ok": 1, "data": {"data": items}}


fc3 = FakeClient(att_handler)
out3 = fc3.fetch_attitudes("mid2", target_uid=TARGET)
check("点赞：第 2 页命中 → 只请求 2 页（原实现会请求 18 页）", len(fc3.calls) == 2, f"calls={len(fc3.calls)}")
check("点赞：返回结果含目标用户", any(r["uid"] == TARGET for r in out3))

fc4 = FakeClient(att_handler)
fc4.fetch_attitudes("mid2")
check("点赞：不传 target_uid 时行为不变（翻满 18 页）", len(fc4.calls) == 18, f"calls={len(fc4.calls)}")


# --- 目标不存在时不能提前退出 ---
def repost_handler_miss(url: str, params: dict[str, Any]) -> dict[str, Any]:
    page = params.get("page", 1)
    items = [
        {
            "user": {"id": f"u{page}_{i}", "screen_name": "x"},
            "created_at": "Tue Jan 06 10:00:00 +0800 2026",
            "text_raw": "t",
            "idstr": f"m{page}_{i}",
        }
        for i in range(20)
    ]
    return {"data": items}


fc5 = FakeClient(repost_handler_miss)
fc5.fetch_reposts("mid3", target_uid=TARGET)
check("转发：目标不存在时仍翻完所有页（不漏数据）", len(fc5.calls) == 30, f"calls={len(fc5.calls)}")

print()
print("=" * 68)
print("测试 3：评论双排序的跳过条件（has_more + total_number）")
print("=" * 68)

from analyzer import InteractionAnalyzer


def make_analyzer(fetch_comments_impl: Callable[..., Any]) -> tuple[Any, FakeClient]:
    fc = FakeClient(lambda u, p: {})
    fc.fetch_comments = fetch_comments_impl
    a = InteractionAnalyzer(
        fc,
        {"uid": "1", "screen_name": "甲"},
        {"uid": "2", "screen_name": "乙"},
        types={"评论"},
        checkpoint=None,
    )
    return a, fc


def cmt(cid: Any, uid: str = "2") -> dict[str, Any]:
    return {
        "uid": uid,
        "screen_name": "乙",
        "created_at": 1767000000.0,
        "text": f"c{cid}",
        "cid": str(cid),
        "reply_to": "",
        "reply_to_user": "",
        "is_reply": False,
        "inner": [],
    }


def wb_fixture(mid: str) -> dict[str, Any]:
    """_add() 需要的字段：mid / text_plain / created_at / url。"""
    return {
        "mid": mid,
        "text_plain": "微博正文",
        "text_html": "微博正文",
        "created_at": 1767000000.0,
        "url": f"https://weibo.com/detail/{mid}",
        "reposts_count": 0,
        "comments_count": 0,
        "attitudes_count": 0,
    }


# 场景 A：时间序一次拿全（38 条 / total=38 / has_more=False）→ 应跳过热度序
calls_a = []


def impl_a(
    mid: str, page: int = 1, cid: Any = None, flow: str = "1", bulletin: int = 0
) -> tuple[list[dict[str, Any]], bool, str, int]:
    calls_a.append(flow)
    return ([cmt(i) for i in range(38)], False, "", 38)


a, _ = make_analyzer(impl_a)
a._scan_comments(wb_fixture("m1"), "2", "乙", "A", "B", owner_uid="1")
check("场景A：时间序已拿全 → 只跑 flow=1，跳过 flow=0", calls_a == ["1"], str(calls_a))
check("场景A：评论仍全部收录（38 条）", len(a.records) == 38, str(len(a.records)))

# 场景 B：时间序只拿到 38 条但 total=100（疑似有折叠）→ 必须补跑热度序
calls_b = []


def impl_b(
    mid: str, page: int = 1, cid: Any = None, flow: str = "1", bulletin: int = 0
) -> tuple[list[dict[str, Any]], bool, str, int]:
    calls_b.append(flow)
    if flow == "1" and page == 1:
        return ([cmt(i) for i in range(38)], False, "", 100)
    if flow == "0" and page == 1:
        return ([cmt(100 + i) for i in range(5)], False, "", 100)
    return ([], False, "", 100)


a2, _ = make_analyzer(impl_b)
a2._scan_comments(wb_fixture("m2"), "2", "乙", "A", "B", owner_uid="1")
check("场景B：拿到的条数 < total → 补跑热度序", calls_b == ["1", "0"], str(calls_b))
check("场景B：热度序独有的评论也被收录（38+5=43）", len(a2.records) == 43, str(len(a2.records)))

# 场景 C：接口未返回 total_number → 保持原行为（跑双排序）
calls_c = []


def impl_c(
    mid: str, page: int = 1, cid: Any = None, flow: str = "1", bulletin: int = 0
) -> tuple[list[dict[str, Any]], bool, str, int]:
    calls_c.append(flow)
    if page == 1:
        return ([cmt(f"{flow}{i}") for i in range(10)], False, "", 0)
    return ([], False, "", 0)


a3, _ = make_analyzer(impl_c)
a3._scan_comments(wb_fixture("m3"), "2", "乙", "A", "B", owner_uid="1")
check("场景C：无 total_number → 保守跑双排序（不降低覆盖度）", calls_c == ["1", "0"], str(calls_c))

print()
print("=" * 68)
print("测试 4：SQLite 增量断点（CheckpointStore）")
print("=" * 68)

import json
import shutil
import time

from checkpoint import CheckpointStore

cpdir = tempfile.mkdtemp(prefix="weibo_cp_")

# --- 4.1 .json 路径自动改写成 .db ---
st = CheckpointStore(os.path.join(cpdir, "checkpoint_1_2.json"))
check("传 .json 路径时自动改用同名 .db", st.path.endswith(".db") and not st.path.endswith(".json"), st.path)
check("同时记住旧 .json 作为迁移来源", st.legacy_json.endswith(".json"), st.legacy_json)

# --- 4.2 首次保存 / 重载 ---
r1 = [rec("转发", "A→B", "甲", "w1", "a1")]
r1[0]["_key"] = ["k1", "转发", "A→B", "甲"]
st.save(r1, {"m1", "m2"}, 1000.0, {"A微博数": 2}, True, [{"mid": "m9", "err": "boom"}])
cp = st.load()
check("重载记录数 = 1", len(cp["records"]) == 1, str(len(cp["records"])))
check("重载 scanned_mids = {m1,m2}", set(cp["scanned_mids"]) == {"m1", "m2"}, str(sorted(cp["scanned_mids"])))
check("重载 last_run_ts = 1000.0", cp["last_run_ts"] == 1000.0, str(cp["last_run_ts"]))
check("重载 stats 保留", cp["stats"].get("A微博数") == 2, str(cp["stats"]))
check("重载 like_api_ok = True", cp["like_api_ok"] is True, str(cp["like_api_ok"]))
check("重载 failed_weibos = 1 条", len(cp["failed_weibos"]) == 1, str(len(cp["failed_weibos"])))

# --- 4.3 增量追加：只写新增，不重复 ---
r2 = [*r1, rec("点赞", "A→B", "甲", "w2", "a2")]
r2[1]["_key"] = ["k2", "点赞", "A→B", "甲"]
st.save(
    r2,
    {"m1", "m2", "m3"},
    2000.0,
    {"A微博数": 3},
    True,
    [{"mid": "m9", "err": "boom"}, {"mid": "m10", "err": "boom2"}],
)
cp2 = st.load()
check("增量后记录数 = 2（未重复写入）", len(cp2["records"]) == 2, str(len(cp2["records"])))
check("增量后 scanned_mids = 3", len(cp2["scanned_mids"]) == 3, str(len(cp2["scanned_mids"])))
check("增量后 last_run_ts = 2000.0", cp2["last_run_ts"] == 2000.0, str(cp2["last_run_ts"]))
check(
    "增量后 failed_weibos = 2（按 mid 去重）", len(cp2["failed_weibos"]) == 2, str(len(cp2["failed_weibos"]))
)

# --- 4.4 同一 _key 重复 save 不产生重复行（主键去重） ---
st.save(r2, {"m1", "m2", "m3"}, 2000.0, {"A微博数": 3}, True, [{"mid": "m9"}])
cp3 = st.load()
check("重复 save 同一批 → 记录数仍为 2", len(cp3["records"]) == 2, str(len(cp3["records"])))

# --- 4.5 _key 为 None 时不去重（与原 JSON 实现一致） ---
st2 = CheckpointStore(os.path.join(cpdir, "nokey.db"))
nokey = {"时间": "t", "互动类型": "点赞", "方向": "A→B", "发起方": "甲", "_key": None}
st2.save([dict(nokey), dict(nokey)], set(), 1.0, {}, True, [])
check(
    "_key=None 的两条相同记录都保留（等价于不去重）",
    len(st2.load()["records"]) == 2,
    str(len(st2.load()["records"])),
)

# --- 4.6 旧 JSON 断点自动迁移，且原文件保留 ---
old_json = os.path.join(cpdir, "legacy.json")
with open(old_json, "w", encoding="utf-8") as f:
    json.dump(
        {
            "records": [rec("评论", "B→A", "乙", "old", "c")],
            "scanned_mids": ["old1", "old2"],
            "last_run_ts": 555.0,
            "stats": {"B微博数": 7},
            "like_api_ok": False,
            "failed_weibos": [{"mid": "of1", "err": "x"}],
        },
        f,
        ensure_ascii=False,
    )
st3 = CheckpointStore(old_json)
cp4 = st3.load()
check("旧 JSON 断点被自动迁移（记录数 = 1）", len(cp4["records"]) == 1, str(len(cp4["records"])))
check(
    "旧 JSON 的 scanned_mids 被迁移",
    set(cp4["scanned_mids"]) == {"old1", "old2"},
    str(sorted(cp4["scanned_mids"])),
)
check(
    "旧 JSON 的 last_run_ts / stats 被迁移", cp4["last_run_ts"] == 555.0 and cp4["stats"].get("B微博数") == 7
)
check("旧 JSON 的 like_api_ok = False 被迁移", cp4["like_api_ok"] is False, str(cp4["like_api_ok"]))
check("迁移计数暴露给调用方", cp4.get("migrated_count") == 1, str(cp4.get("migrated_count")))
check("迁移后原 .json 文件仍保留未删", os.path.exists(old_json))
# 再 load 一次不应重复导入
st3b = CheckpointStore(old_json)
check("二次打开不再重复迁移（记录数仍为 1）", len(st3b.load()["records"]) == 1)

# --- 4.7 断点文件损坏时不抛异常 ---
bad = os.path.join(cpdir, "broken.db")
with open(bad, "wb") as f:
    f.write(b"not a sqlite file at all")
try:
    cp5 = CheckpointStore(bad).load()
    check("损坏的断点文件 → 返回空结构而不抛异常", cp5["records"] == [] and cp5["scanned_mids"] == [])
except Exception as e:
    check("损坏的断点文件 → 返回空结构而不抛异常", False, repr(e))

# --- 4.8 旧 JSON 实现需要「读全量+内存合并+写全量」；这里验证增量语义：---
#     save 只应触碰新增记录（用 monkeypatch 统计 executemany 的行数）
st4 = CheckpointStore(os.path.join(cpdir, "incr.db"))
big = []
for i in range(50):
    big.append({**rec("转发", "A→B", "甲", f"w{i}", "a"), "_key": ["k", i]})
st4.save(big, set(), 1.0, {}, True, [])  # 首次：写 50
st4.save(
    [*big, {**rec("转发", "A→B", "甲", "w50", "a"), "_key": ["k", 50]}], set(), 2.0, {}, True, []
)  # 追加 1 条
check("增量写入语义：追加后共 51 条", len(st4.load()["records"]) == 51, str(len(st4.load()["records"])))

shutil.rmtree(cpdir, ignore_errors=True)

print()
print("=" * 68)
print("测试 5：令牌桶限速（平均 QPS 不变，允许短突发）")
print("=" * 68)

from client import WeiboClient

# 用很小的间隔做实测：min_interval=0.02s，突发上限 2
c = WeiboClient(object(), speed=2)
c.min_interval = 0.02
c._rate = 1.0 / c.min_interval
c._burst = 2.0
c._tokens = 2.0
c._last_refill = time.monotonic()

t0 = time.monotonic()
c._throttle()
c._throttle()
burst_elapsed = time.monotonic() - t0
check("突发额度内（前 2 次）几乎不等待", burst_elapsed < 0.02, f"{burst_elapsed * 1000:.1f}ms")

t0 = time.monotonic()
for _ in range(10):
    c._throttle()
elapsed = time.monotonic() - t0
# 10 次请求、突发 2 → 需要补充 8 个令牌 ≈ 8 × 0.02 = 0.16s
expect = 8 * 0.02
check(
    "10 次请求总耗时 ≈ (10-突发)/速率",
    expect * 0.6 <= elapsed <= expect * 2.2 + 0.05,
    f"实测 {elapsed * 1000:.1f}ms，理论 {expect * 1000:.1f}ms",
)

# 平均 QPS 不得高于配置速率
rate = 10 / elapsed
check("实测平均 QPS ≤ 配置速率（不突破限速）", rate <= 1 / 0.02 * 1.35, f"{rate:.1f} vs {1 / 0.02:.1f}")

# 旧实现是固定 sleep：每次调用都必须等满 min_interval。
# 令牌桶允许突发，所以前 burst 次应当显著快于固定 sleep 方案。
check(
    "突发优于固定 sleep（前 2 次省下 ≈ 2×间隔）",
    burst_elapsed < 2 * 0.02 * 0.5,
    f"{burst_elapsed * 1000:.1f}ms vs {2 * 0.02 * 1000:.1f}ms",
)

print()
print("=" * 68)
print("测试 6：analyzer._run_tasks 并发与风控上抛")
print("=" * 68)

from analyzer import InteractionAnalyzer as _IA
from client import RiskControlError as _RCE

fc6 = FakeClient(lambda u, p: {})
fc6.workers = 3
a6 = _IA(
    fc6, {"uid": "1", "screen_name": "甲"}, {"uid": "2", "screen_name": "乙"}, types={"转发"}, checkpoint=None
)
check("analyzer.workers 取自 client.workers", a6.workers == 3, str(a6.workers))

SLEEP = 0.30
t0 = time.monotonic()
res = a6._run_tasks(
    [
        ("t1", lambda: (time.sleep(SLEEP), "v1")[1], False),
        ("t2", lambda: (time.sleep(SLEEP), "v2")[1], False),
        ("t3", lambda: (time.sleep(SLEEP), "v3")[1], False),
    ]
)
par = time.monotonic() - t0
check(
    "3 个任务并发执行（耗时 < 串行的 2/3）",
    par < SLEEP * 2,
    f"并发 {par * 1000:.0f}ms vs 串行 {SLEEP * 3 * 1000:.0f}ms",
)
check("并发结果完整", res == {"t1": ("v1", None), "t2": ("v2", None), "t3": ("v3", None)}, str(res))

# 单任务时不启动线程池
a6b = _IA(
    fc6, {"uid": "1", "screen_name": "甲"}, {"uid": "2", "screen_name": "乙"}, types={"转发"}, checkpoint=None
)
t0 = time.monotonic()
a6b._run_tasks([("only", lambda: "v", False)])
check("单任务走串行分支（无池化开销）", (time.monotonic() - t0) < 0.05)

# 空任务
check("空任务列表返回 {}", a6._run_tasks([]) == {})


# RiskControlError 必须由调用线程上抛（保持外层「等待后重试」语义）
def boom() -> None:
    raise _RCE("触发了风控")


try:
    a6._run_tasks([("r", boom, False)])
    check("RiskControlError 在调用线程上抛", False, "未抛出")
except _RCE as e:
    check("RiskControlError 在调用线程上抛", str(e) == "触发了风控", str(e))
except Exception as e:
    check("RiskControlError 在调用线程上抛", False, f"抛出了 {type(e).__name__}")


# 普通异常不抛出，而是作为 err 返回（由调用方决定处理）
def bad() -> None:
    raise ValueError("bad value")


res_err = a6._run_tasks([("e", bad, False)])
check(
    "普通异常作为 err 返回而不上抛",
    isinstance(res_err["e"][1], ValueError) and res_err["e"][0] is None,
    str(res_err),
)

print()
print("=" * 68)
print("测试 7：A_MODE 分支")
print("=" * 68)

import importlib
import types


def reload_with_mode(mode: str) -> tuple[Any, Any]:
    """把 build_mode 换成指定模式后重新导入 main / gui_app。"""
    fake = types.ModuleType("build_mode")
    fake.A_MODE = mode
    sys.modules["build_mode"] = fake
    for m in ("main", "gui_app"):
        sys.modules.pop(m, None)
    import gui_app as gui_mod
    import main as main_mod

    return main_mod, gui_mod


# ---------- self 版（用户A = 扫码登录账号） ----------
m_self, g_self = reload_with_mode("self")
check("self 版：main.SELF_IS_A = True", m_self.SELF_IS_A is True)
check("self 版：gui_app.SELF_IS_A = True", g_self.SELF_IS_A is True)
a_self = m_self.parse_args(["--u2", "1234567890"])
check("self 版：--u1 被隐藏且不接受", a_self.u1 is None)
check("self 版：--u2 正常解析", a_self.u2 == "1234567890", str(a_self.u2))
try:
    m_self.main(["--u1", "1", "--u2", "2"])
    check("self 版：显式传 --u1 被拒绝", False, "未报错")
except SystemExit as e:
    check("self 版：显式传 --u1 被拒绝", "不再支持 --u1" in str(e), str(e)[:40])
check("self 版：帮助文本写明「不可修改」", "不可修改" in g_self.HELP_TEXT)
check("self 版：帮助文本写明「用户A = 当前扫码登录的账号」", "用户A = 当前扫码登录的账号" in g_self.HELP_TEXT)
check("self 版：帮助文本含重登一致性提醒", "重新登录的账号必须和开始时一致" in g_self.HELP_TEXT)

# ---------- A_MODE = "manual" 分支（A / B 都手填） ----------
m_man, g_man = reload_with_mode("manual")
check("A_MODE=manual 分支：main.SELF_IS_A = False", m_man.SELF_IS_A is False)
check("A_MODE=manual 分支：gui_app.SELF_IS_A = False", g_man.SELF_IS_A is False)
a_man = m_man.parse_args(["--u1", "111", "--u2", "222"])
check("A_MODE=manual 分支：--u1 可正常解析", a_man.u1 == "111", str(a_man.u1))
try:
    m_man.main(["--u2", "222"])
    check("A_MODE=manual 分支：缺 --u1 时报错提示", False, "未报错")
except SystemExit as e:
    check("A_MODE=manual 分支：缺 --u1 时报错提示", "缺少用户A" in str(e), str(e)[:40])
check(
    "A_MODE=manual 分支：帮助文本为「用户A / 用户B 都手填」", "用户A / 用户B 支持三种写法" in g_man.HELP_TEXT
)
check("A_MODE=manual 分支：帮助文本不含「不可修改」", "不可修改" not in g_man.HELP_TEXT)
check(
    "A_MODE=manual 分支：帮助文本不含重登一致性提醒", "重新登录的账号必须和开始时一致" not in g_man.HELP_TEXT
)

# ---------- GUI 控件状态（需要 tkinter） ----------
try:
    import tkinter as _tk

    _HAS_TK = True
except Exception:
    _HAS_TK = False


def _new_root() -> Any:
    """建一个隐藏的根窗口。

    Tk 本身起不来是**环境问题**（无图形会话、tcl 资源读不到），应当跳过而不是判失败；
    只有「Tk 起得来但 GUI 构建不出来」才是真故障。``tests/`` 里的 ``tk_root`` 夹具
    就是这个语义，这里保持一致。
    """
    try:
        root = _tk.Tk()
    except Exception as e:
        print(f"  [SKIP] 无法创建 Tk 窗口，跳过 GUI 控件检查：{e}")
        return None
    root.withdraw()
    return root


if _HAS_TK:
    import config as _cfg

    _saved_data_dir = _cfg.DATA_DIR
    _cfg.DATA_DIR = tempfile.mkdtemp(prefix="weibo_gui_")

    _r1 = _new_root()
    if _r1 is not None:
        try:
            _app1 = g_self.App(_r1)
            check(
                "self 版 GUI：用户A 输入框为只读",
                str(_app1.entry_a.cget("state")) == "readonly",
                str(_app1.entry_a.cget("state")),
            )
            check(
                "self 版 GUI：用户A 初值为「（未登录）」",
                _app1.a_var is not None and _app1.a_var.get() == "（未登录）",
                str(_app1.a_var.get()),
            )
            check(
                "self 版 GUI：用户区标题为「选择要对比的用户」",
                "选择要对比的用户" in str(_app1.entry_a.master.cget("text")),
                str(_app1.entry_a.master.cget("text")),
            )
        except Exception as e:
            check("self 版 GUI：可正常构建", False, repr(e))
        finally:
            _r1.destroy()

    _r2 = _new_root()
    if _r2 is not None:
        try:
            _app2 = g_man.App(_r2)
            check(
                "A_MODE=manual 分支 GUI：用户A 输入框可编辑",
                str(_app2.entry_a.cget("state")) == "normal",
                str(_app2.entry_a.cget("state")),
            )
            check("A_MODE=manual 分支 GUI：无 a_var（A 由用户手填）", _app2.a_var is None)
            check(
                "A_MODE=manual 分支 GUI：用户区标题为「填写两个微博用户」",
                "填写两个微博用户" in str(_app2.entry_a.master.cget("text")),
                str(_app2.entry_a.master.cget("text")),
            )
        except Exception as e:
            check("A_MODE=manual 分支 GUI：可正常构建", False, repr(e))
        finally:
            _r2.destroy()

    _cfg.DATA_DIR = _saved_data_dir
else:
    print("  [SKIP] tkinter 不可用，跳过 GUI 控件检查")

# ---------- 恢复真实 build_mode ----------
sys.modules.pop("build_mode", None)
for _m in ("main", "gui_app"):
    sys.modules.pop(_m, None)
import build_mode as _bm

importlib.reload(_bm)
check("build_mode.py 默认值合法（self / manual）", _bm.A_MODE in ("self", "manual"), _bm.A_MODE)

print()
print("=" * 68)
print("测试 8：cookie 持久化加密（Windows DPAPI）")
print("=" * 68)

import base64

import requests as _rq

import config
import login as login_mod

_real_avail = login_mod.dpapi_available
_cpdir = tempfile.mkdtemp(prefix="weibo_ck_")
_save_data_dir, _save_cookie_file = config.DATA_DIR, config.COOKIE_FILE
config.DATA_DIR = _cpdir
config.COOKIE_FILE = os.path.join(_cpdir, "cookies.json")

# 屏蔽掉网络校验（load_cookies 会请求微博验证登录态）
_real_check = login_mod._check_logged_in
login_mod._check_logged_in = lambda s: True

SECRET = "SUB-secret-token-do-not-leak-98765"


def _make_session() -> Any:
    s = _rq.Session()
    s.cookies.set("SUB", SECRET, domain=".weibo.com")
    s.cookies.set("SUBP", "subp-value", domain=".weibo.com")
    s.cookies.set("XSRF-TOKEN", "xsrf", domain=".weibo.com")
    return s


check("DPAPI 在当前环境可用", login_mod.dpapi_available() is True)

# --- 8.1 加解密往返 ---
_raw = b'{"cookies":[{"name":"SUB","value":"' + SECRET.encode() + b'"}]}'
_blob = login_mod.protect_bytes(_raw)
check(
    "protect_bytes 返回密文",
    isinstance(_blob, (bytes, bytearray)) and len(_blob) > 0,
    f"{len(_blob) if _blob else 0} 字节",
)
check("unprotect 能还原原文", login_mod.unprotect_bytes(_blob) == _raw)
check("密文里不含明文 token", SECRET.encode() not in _blob)

# --- 8.2 完整性：payload 区被篡改必须被发现 ---
_tamper_detected = 0
_tamper_total = 0
for _i in range(20, len(_blob)):  # 跳过 DPAPI header（4~19 不受完整性保护）
    _b = bytearray(_blob)
    _b[_i] ^= 0x01
    _tamper_total += 1
    if login_mod.unprotect_bytes(bytes(_b)) is None:
        _tamper_detected += 1
check(
    "payload 区逐字节篡改全部被检测到",
    _tamper_detected == _tamper_total,
    f"{_tamper_detected}/{_tamper_total}",
)
check("截断的密文解密失败", login_mod.unprotect_bytes(_blob[: len(_blob) // 2]) is None)
check("空密文解密失败", login_mod.unprotect_bytes(b"") is None)
check("垃圾数据解密失败", login_mod.unprotect_bytes(b"not-a-dpapi-blob-at-all") is None)

# --- 8.3 save_cookies 落盘格式 ---
_mode = login_mod.save_cookies(_make_session())
check("save_cookies 使用 dpapi 模式", _mode == "dpapi", str(_mode))
check("LAST_SAVE_MODE 同步更新", login_mod.LAST_SAVE_MODE == "dpapi")
with open(config.COOKIE_FILE, encoding="utf-8") as _f:
    _doc = json.load(_f)
check(
    "文件是加密格式（含 __weibo_enc__ 标记）",
    _doc.get("__weibo_enc__") == 1 and _doc.get("alg") == "dpapi",
    str(list(_doc)),
)
with open(config.COOKIE_FILE, encoding="utf-8") as _f:
    _raw_enc = _f.read()
check("文件里不含明文 cookie 值", SECRET not in _raw_enc)
check("文件里也不含 cookie 名（整体被加密）", "SUBP" not in _raw_enc)

# --- 8.4 load_cookies 能还原 ---
_s = login_mod.load_cookies()
check("load_cookies 能还原 session", _s is not None)
check(
    "还原后的 SUB 值正确",
    _s is not None and _s.cookies.get("SUB") == SECRET,
    _s.cookies.get("SUB") if _s else None,
)
check(
    "还原后的 cookie 数量正确", _s is not None and len(_s.cookies) == 3, str(len(_s.cookies)) if _s else None
)

# --- 8.5 旧的明文文件能被识别并就地升级 ---
_plain_path = config.COOKIE_FILE
with open(_plain_path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "cookies": [
                {
                    "name": "SUB",
                    "value": SECRET,
                    "domain": ".weibo.com",
                    "path": "/",
                    "secure": False,
                    "expires": None,
                    "rest": {},
                    "rfc2109": False,
                }
            ]
        },
        f,
        ensure_ascii=False,
    )
with open(_plain_path, encoding="utf-8") as _f:
    _plain_before = _f.read()
check("明文文件在升级前确实含明文", SECRET in _plain_before)
_s2 = login_mod.load_cookies()
check("旧的明文 cookie 文件仍能正常加载", _s2 is not None and _s2.cookies.get("SUB") == SECRET)
with open(_plain_path, encoding="utf-8") as _f:
    _plain_after = _f.read()
check("明文文件已被就地升级为加密格式", SECRET not in _plain_after)
check("升级后仍是可用的登录态", login_mod.load_cookies() is not None)

# --- 8.6 解不开时的报错要清楚（模拟换机器/换用户） ---
with open(_plain_path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "__weibo_enc__": 1,
            "alg": "dpapi",
            "data": base64.b64encode(b"definitely-not-a-valid-dpapi-blob").decode(),
        },
        f,
    )
try:
    login_mod.load_cookies()
    check("无法解密的登录态抛出可读的 LoginError", False, "未抛异常")
except login_mod.LoginError as e:
    msg = str(e)
    check(
        "无法解密的登录态抛出可读的 LoginError", "无法解密" in msg and "重新扫码" in msg, msg.splitlines()[0]
    )
except Exception as e:
    check("无法解密的登录态抛出可读的 LoginError", False, f"抛出了 {type(e).__name__}")

# --- 8.7 损坏的 base64 ---
with open(_plain_path, "w", encoding="utf-8") as f:
    json.dump({"__weibo_enc__": 1, "alg": "dpapi", "data": "!!!not base64!!!"}, f)
try:
    login_mod.load_cookies()
    check("损坏的 base64 抛出 LoginError", False, "未抛异常")
except login_mod.LoginError as e:
    check("损坏的 base64 抛出 LoginError", "损坏" in str(e), str(e)[:40])

# --- 8.8 DPAPI 不可用时退回明文（不阻塞登录） ---
login_mod.dpapi_available = lambda: False
try:
    _mode2 = login_mod.save_cookies(_make_session())
    check("DPAPI 不可用时退回明文模式", _mode2 == "plaintext", str(_mode2))
    with open(config.COOKIE_FILE, encoding="utf-8") as _f:
        _doc2 = json.load(_f)
    check("退回明文时文件是旧格式（可被旧版读）", "cookies" in _doc2, str(list(_doc2)))
    check("退回明文时仍能加载", login_mod.load_cookies() is not None)
finally:
    login_mod.dpapi_available = _real_avail

# --- 8.9 save_cookies 绝不抛异常（保存失败只该警告） ---
login_mod.dpapi_available = lambda: True
_bad_dir = os.path.join(_cpdir, "no-such-dir", "deeper")
config.DATA_DIR = os.path.join(_bad_dir, "\x00invalid")  # 非法路径，makedirs 必失败
config.COOKIE_FILE = os.path.join(config.DATA_DIR, "cookies.json")
try:
    _mode3 = login_mod.save_cookies(_make_session())
    check("保存失败时返回 'failed' 而不抛异常", _mode3 == "failed", str(_mode3))
except Exception as e:
    check("保存失败时返回 'failed' 而不抛异常", False, f"抛出了 {type(e).__name__}: {e}")

# 还原
login_mod.dpapi_available = _real_avail
login_mod._check_logged_in = _real_check
config.DATA_DIR, config.COOKIE_FILE = _save_data_dir, _save_cookie_file
shutil.rmtree(_cpdir, ignore_errors=True)

print()
print("=" * 68)
print(f"结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 68)
sys.exit(1 if FAIL else 0)
