"""生成展示站用的样例报告。

调用**真实的** exporter.export()，只是把输入换成合成数据 ——
所以产出就是程序实际会导出的那种 HTML 日志，不是另画一版。

用法：
    python make_sample_report.py
产出：
    site/sample/微博互动_样例_报告.html   （自包含单文件）
"""

import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exporter

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "sample")

A = {"uid": "1000000001", "screen_name": "林知远"}
B = {"uid": "1000000002", "screen_name": "沈屿舟"}

# 用固定种子，保证每次生成的样例一致（便于对比 / 进版本库）
random.seed(20260916)

WB_A = [
    "整理了一下这学期分布式系统的课程笔记，Raft 那部分踩的坑最多，改天单独写一篇。",
    "都柏林终于放晴了，图书馆四楼窗边位置今天很难抢。",
    "把毕设的数据管道重写了一遍，从 pandas 换成 openpyxl + csv，打包体积直接少了 19MB。",
    "第一次用 PyInstaller 打包带 tkinter 的程序，tcl/tk 的目录结构绕了半天才搞明白。",
    "看完《Designing Data-Intensive Applications》第 5 章，对复制这块终于有了整体认识。",
    "周末去 Howth 走了那条悬崖步道，风大到站不稳，但景色确实值。",
    "面了一家做支付的公司，问了一堆并发和幂等的问题，感觉答得一般。",
    "终于把断点续传从 JSON 换成 SQLite 了，每次保存不用再重写整个文件。",
]
WB_B = [
    "今天调了一天的接口，最后发现是对方的时区处理有问题，白忙一场。",
    "推荐一个查 Git 提交历史的工具，比 git log --graph 好用不少。",
    "公司楼下的咖啡换供应商了，味道居然变好了。",
    "把项目里的 pandas 依赖干掉了，CI 时间从 6 分钟降到 2 分半。",
    "重新看了一遍 TCP 拥塞控制，慢启动那块以前一直是一知半解。",
    "年底了，又开始有人问我要不要跳槽。",
    "写了个脚本自动整理下载文件夹，跑完发现删掉的全是我自己要用的。",
    "都柏林今年的雨比去年多，感觉每天都在下雨。",
]

TEXT_REPOST = [
    "说得对，这块确实容易踩坑。",
    "学习了，感谢分享。",
    "同感，我也遇到过一模一样的问题。",
    "收藏了，回头细看。",
    "这个思路很清晰。",
    "转发一下，正好有朋友需要。",
]
TEXT_COMMENT = [
    "想问下你当时是怎么定位到这个问题的？",
    "这个观点我有不同看法，感觉还有别的可能。",
    "写得很清楚，比官方文档好懂。",
    "所以最后是怎么解决的？",
    "哈哈这个我懂。",
    "我上周也刚看完这一章，确实写得好。",
    "有没有推荐的入门资料？",
]
TEXT_REPLY = [
    "主要是看日志里返回的时间戳不对，才发现是时区的问题。",
    "确实，我漏了并发那一层。",
    "回头我把细节整理一下发出来。",
    "推荐直接看官方那篇 RFC，比二手资料准。",
    "嗯，下次注意。",
]


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_records():
    """构造覆盖 全部类型 × 两个方向 × 跨若干月份 的记录。"""
    recs = []
    # 从 2026-03 到 2026-09，每月若干条，保证月度趋势图有内容
    months = [(2026, m) for m in range(3, 10)]
    key = 0

    for y, m in months:
        n = random.randint(5, 9)
        for _ in range(n):
            day = random.randint(1, 28)
            base = datetime(y, m, day, random.randint(8, 23), random.randint(0, 59))
            a2b = random.random() < 0.5
            owner, other = (A, B) if a2b else (B, A)
            direction = "A→B" if a2b else "B→A"
            wb_text = random.choice(WB_A if a2b else WB_B)
            wb_time = base - timedelta(days=random.randint(0, 6), hours=random.randint(0, 20))
            kind = random.choices(["转发", "评论", "评论回复", "点赞"], weights=[3, 4, 3, 3])[0]

            if kind == "转发":
                act, rp_to, rp_txt = random.choice(TEXT_REPOST), "", ""
            elif kind == "评论":
                act, rp_to, rp_txt = random.choice(TEXT_COMMENT), "", ""
            elif kind == "评论回复":
                act = random.choice(TEXT_REPLY)
                rp_to = owner["screen_name"]
                rp_txt = random.choice(TEXT_COMMENT)
            else:
                act, rp_to, rp_txt = "点赞了这条微博", "", ""

            recs.append(
                {
                    "时间": _ts(base),
                    "互动类型": kind,
                    "方向": direction,
                    "发起方": other["screen_name"],
                    "微博作者": owner["screen_name"],
                    "微博内容": wb_text,
                    "互动内容": act,
                    "被回复人": rp_to,
                    "被回复评论": rp_txt,
                    "微博时间": _ts(wb_time),
                    "微博链接": f"https://weibo.com/{owner['uid']}/sample{key}",
                    "互动链接": f"https://weibo.com/u/{other['uid']}",
                    "_key": ["sample", key],
                }
            )
            key += 1

    recs.sort(key=lambda r: r["时间"])
    return recs


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    recs = build_records()
    res = exporter.export(
        recs,
        A,
        B,
        out_dir=OUT_DIR,
        formats={"html"},
        failed_weibos=None,
        enable_refresh=False,
    )
    html = res["html"]
    # 给一个稳定的文件名（exporter 默认带时间戳，每次生成都会变）
    final = os.path.join(OUT_DIR, "sample-report.html")
    if os.path.exists(final):
        os.remove(final)
    os.replace(html, final)

    size = os.path.getsize(final)
    print(f"记录数：{res['count']}")
    print(f"样例报告：{final}  ({size / 1024:.1f} KB)")
    with open(final, encoding="utf-8") as f:
        content = f.read()
    self_contained = 'src="http' not in content
    print(f"自包含单文件：{'是' if self_contained else '否（有外链资源）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
