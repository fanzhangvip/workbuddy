# -*- coding: utf-8 -*-
"""兴趣点提取的**纯逻辑**部分：词表校验 / 二级标签归一 / 契约校验 / 证据回链（t0）。

    .venv\\Scripts\\python.exe tests\\test_interest_taxonomy.py

## 守的是什么

这条路线的技术选择是「**受控一级分类 + 自由二级标签 + 证据必须回链**」，不用
embedding、不做聚类、不加依赖。它的可靠性全部押在三件事上，本文件把它们钉住：

1. **词表是静态数据=代码**：key 写错、重复、类别太少、忘了 `other` 兜底 —— 这些
   单测很难从业务行为上发现（只会表现为"图上少一类"或"模型老是触发修复重试"），
   必须在**加载期**拦；
2. **一级必须受控**：模型写了枚举以外的值要**被契约拒绝**（从而触发回灌修复），
   而不是一路流进库里变成图上多出来的一类；
3. **没有证据的兴趣点不许进图**：回链不上就丢弃并计数 —— 否则图上会出现一条
   点开看不到原话、无法核对的"兴趣"。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import sys

from _harness import ROOT, check, summary  # noqa: E402

import interest  # noqa: E402


def _tax(primaries=None, aliases=None, version="i9.9"):
    return {"taxonomy_version": version,
            "primaries": primaries if primaries is not None else [
                {"key": "a_one", "name": "甲", "desc": "d"},
                {"key": "b_two", "name": "乙", "desc": "d"},
                {"key": "other", "name": "其它", "desc": "d"},
            ],
            "aliases": aliases or {}}


def main():
    # ---------------- ① 词表校验 ----------------
    check("真词表通过校验", interest.validate_taxonomy(interest.load_taxonomy()) == [],
          str(interest.validate_taxonomy(interest.load_taxonomy())))
    check("类别太少被拒", any("至少 3 类" in e for e in interest.validate_taxonomy(_tax([
        {"key": "a_one", "name": "甲", "desc": "d"}]))))
    check("缺 key / key 重复 / 缺 name 都被拒",
          any("缺 key" in e for e in interest.validate_taxonomy(_tax([
              {"key": "", "name": "甲", "desc": "d"},
              {"key": "b_two", "name": "乙", "desc": "d"},
              {"key": "other", "name": "其它", "desc": "d"}])))
          and any("key 重复" in e for e in interest.validate_taxonomy(_tax([
              {"key": "a_one", "name": "甲", "desc": "d"},
              {"key": "a_one", "name": "乙", "desc": "d"},
              {"key": "other", "name": "其它", "desc": "d"}])))
          and any("缺 name" in e for e in interest.validate_taxonomy(_tax([
              {"key": "a_one", "name": "", "desc": "d"},
              {"key": "b_two", "name": "乙", "desc": "d"},
              {"key": "other", "name": "其它", "desc": "d"}]))))
    check("key 里带怪字符被拒（它要进提示词与枚举）",
          any("只能是小写字母" in e for e in interest.validate_taxonomy(_tax([
              {"key": "A 大写", "name": "甲", "desc": "d"},
              {"key": "b_two", "name": "乙", "desc": "d"},
              {"key": "other", "name": "其它", "desc": "d"}]))))
    check("★ 缺 other 兜底类被拒（说不清的兴趣点要有去处）",
          any("other" in e for e in interest.validate_taxonomy(_tax([
              {"key": "a_one", "name": "甲", "desc": "d"},
              {"key": "b_two", "name": "乙", "desc": "d"},
              {"key": "c_three", "name": "丙", "desc": "d"}]))))

    # ---------------- ② 二级标签归一（确定性、不用 embedding）----------------
    t = _tax(aliases={"想转行做算法": "转行做算法", "学 Python": "Python 入门"})
    check("去首尾空白 + 全角转半角",
          interest.normalize_secondary("  Ｐｙｔｈｏｎ  ", t) == "Python",
          repr(interest.normalize_secondary("  Ｐｙｔｈｏｎ  ", t)))
    check("去尾部标点（'。' '？' 都不算信息）",
          interest.normalize_secondary("学多久才够用呢？", t) == "学多久才够用呢")
    check("去包裹的引号/书名号",
          interest.normalize_secondary("「学 Python」", t) == "Python 入门",
          repr(interest.normalize_secondary("「学 Python」", t)))
    check("★ aliases 做同义合并（人工维护，不引相似度阈值）",
          interest.normalize_secondary("想转行做算法", t) == "转行做算法")
    check("★ 侧限 12 字（它要塞进气泡里，不能是一句话）",
          len(interest.normalize_secondary("a" * 40, t)) == 12)
    check("空值不炸", interest.normalize_secondary(None, t) == "")

    # ---------------- ③ 契约：一级必须受控 ----------------
    # ⚠️ 契约里的"受控一级"校验的是**真实词表**（`interests.json`），
    #    所以这里必须用真 key —— 上面那份 `a_one/b_two` 只是给词表校验与归一用的假词表。
    ok_json = ('{"interests":[{"primary":"career_change","secondary":"测试","confidence":0.8,'
               '"evidence_ids":["#001"]}],"summary":"s"}')
    out, err = interest.parse_interests(ok_json)
    check("合法 JSON 解析成功", out is not None and err is None, str(err))
    bad_json = ok_json.replace('"career_change"', '"self_made_category"')
    out2, err2 = interest.parse_interests(bad_json)
    check("★ 自造一级分类被契约拒绝（从而触发回灌修复）",
          out2 is None and err2 is not None
          and "不在受控一级分类里" in str(err2), str(err2)[:120])
    wrapped = "好的，这是结果：\n```json\n" + ok_json + "\n```"
    out3, err3 = interest.parse_interests(wrapped)
    check("模型包了层解释文字也能解析", out3 is not None, str(err3)[:80])

    # ---------------- ④ 证据回链：没有证据的兴趣点丢弃并计数 ----------------
    index = {"#001": "m-1", "#002": "m-2"}
    labels = {"#001": "#001 [10-08 09:12] 客户: 我想转行做算法",
              "#002": "#002 [10-08 09:13] 客户: 想学多久"}
    items = [
        interest.InterestItem(primary="career_change", secondary=" 想转行做算法。",
                              confidence=0.9, evidence_ids=["#001"]),
        interest.InterestItem(primary="skill_learning", secondary="没有证据的猜测",
                              confidence=0.9, evidence_ids=["#999"]),
        interest.InterestItem(primary="skill_learning", secondary="", confidence=0.9,
                              evidence_ids=["#002"]),
    ]
    rows = interest.to_rows(items, t, index, labels)
    check("★ 回链上的留下且带上原话片段",
          len(rows["items"]) == 1 and rows["items"][0]["evidence_message_ids"] == ["m-1"]
          and rows["items"][0]["evidence"] == [labels["#001"]], str(rows))
    check("★ 证据回链不上 → 丢弃", rows["dropped"] == 2, str(rows["dropped"]))
    check("二级标签在落库前已归一", rows["items"][0]["secondary"] == "转行做算法",
          rows["items"][0]["secondary"])

    # ---------------- ⑤ 静态数据必须进镜像（漏拷 = 运行期静默降级）----------------
    # `interests.json` 是**运行期直接读的文件**（`interest.load_taxonomy()`）：漏拷进镜像
    # 不会有任何报错，只会表现为"词表为空"（分析拒绝启动、图是空的），而宿主机上一切正常。
    # 实测踩到过一次 —— 所以这类文件用测试钉住，而不是等线上发现。
    text = open(f"{ROOT}/Dockerfile", encoding="utf-8").read().replace("\\\n", " ")
    copied = {tok for line in text.splitlines() if line.startswith("COPY")
              for tok in line.split()[1:]}
    missing = [f for f in ("rules.json", "interests.json") if f not in copied]
    check("★ 静态数据文件已进镜像（漏拷 = 运行期静默降级）", not missing, str(missing))

    # ---------------- ⑥ 提示词口径：只分析**客户**的话 ----------------
    sys_p = interest.build_system_prompt(interest.load_taxonomy())
    user_p = interest.build_user_prompt("#001 [10-08 09:12] 客户: 我想转行做算法")
    check("★ 提示词声明「只给了客户自己的发言」（口径写在契约里，不靠人记得）",
          "只有客户本人的发言" in sys_p, sys_p[:200])
    check("★ 明确禁止「把课程卖点当成客户的兴趣 / 替客户补全」",
          "不要替客户补全" in sys_p and "课程卖点" in sys_p, "")
    check("用户提示词也说明了输入范围（模型不会去追问不存在的上下文）",
          "客户发言开始" in user_p and "客户" in user_p, user_p[:160])
    return summary()


if __name__ == "__main__":
    sys.exit(main())
