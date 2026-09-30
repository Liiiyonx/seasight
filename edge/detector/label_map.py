"""开放词汇标签 → 四类契约类别的映射表。

为什么需要它
------------
YOLO-World 这类**开放词汇**检测器的输出类别是**提示词原文**
（``"plastic bottle"`` / ``"foam debris"`` / ``"discarded shoe"`` …），
而本项目的输出契约只有四类（``foam`` / ``plastic`` / ``fishing_gear`` / ``other``，
见 ``edge/detector/detector.py`` 的 ``CLASS_NAMES``）。

契约不允许"多出一类"：下游的时序校验（``edge/simulator/simulator.py``）、
MQTT 主题（``docs/mqtt-topics.md``）与平台侧四源对账
（``scripts/check_contract_drift.py``）都按四类写死。
所以开放词汇的自由文本必须在进入契约之前被映射回四类。

设计要点（都是本项目踩过坑之后定下的）
--------------------------------------
1. **显式表，不做模糊匹配。** 映射来自 ``PROMPT_TO_CLASS`` 这张显式表，
   不做词干化、不做相似度猜测 —— 那类"看起来像就映射"的隐式行为，
   出问题时无法在代码里指出是哪一行把 A 判成了 B。
2. **回落必须可观测。** 未命中的词回落 ``other``，但 ``map_prompt()``
   同时返回 ``match_kind``，调用方可以统计 ``fallback_other`` 的数量。
   静默把未知词变成 other，正是本项目反复出现的"静默失效"形状。
3. **穷举由测试守住。** ``DEMO_DEFAULT_CLASSES`` 与 ``CALIBRATION_PROMPTS``
   是历史上真实用过的提示词全集；``test_label_map.py`` 除了逐词断言，
   还用 AST 解析 ``scripts/detect_marine_demo.py`` 里的 ``DEFAULT_CLASSES``，
   一旦上游加了新提示词而这里没跟上，测试直接失败。

本模块**不导入 cv2 / torch / ultralytics**，可单独测试。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 契约类别：必须与 edge/detector/detector.py 的 CLASS_NAMES 逐项一致。
# 这里刻意**不 import** detector（避免为了一个字符串列表拉进 cv2），
# 改用一条对账测试把两处钉在一起（test_label_map.py::test_contract_classes_match_detector）。
# ---------------------------------------------------------------------------
CONTRACT_CLASSES: tuple[str, ...] = ("foam", "plastic", "fishing_gear", "other")

# 未命中映射时的回落类别
UNMAPPED_CLASS = "other"

# match_kind 的取值
MATCH_EXACT = "exact"           # 提示词原文直接命中映射表
MATCH_FALLBACK = "fallback_other"  # 未命中，回落 other


# ---------------------------------------------------------------------------
# 提示词 → 四类。键必须是小写、空白已折叠的形式（见 normalize）。
#
# 分组依据是"现场怎么喊这个东西"，不是模型语义：
#   塑料类 —— 有彩度、成型的塑料制品（瓶/袋/浮球/颗粒）
#   泡沫类 —— EPS 泡沫浮球及其碎片（白亮团状，当地治理优先级最高）
#   渔具类 —— 废旧渔网、绳索、饵料包装
#   other  —— 木板、藻团、生活杂物、鞋等兜底
# ---------------------------------------------------------------------------
PROMPT_TO_CLASS: dict[str, str] = {
    # ---- plastic ----
    "plastic bottle": "plastic",
    "plastic bottles": "plastic",
    "plastic waste": "plastic",
    "plastic debris": "plastic",
    "plastic bag": "plastic",
    "plastic bags": "plastic",
    "plastic cup": "plastic",
    "plastic pellet": "plastic",
    "nurdle": "plastic",
    "buoy": "plastic",
    "float": "plastic",
    # ---- foam ----
    "foam": "foam",
    "foam debris": "foam",
    "foam cup": "foam",
    "foam buoys": "foam",
    "styrofoam": "foam",
    "styrofoam cup": "foam",
    "broken styrofoam cup": "foam",
    "damaged foam cup": "foam",
    "polystyrene": "foam",
    "polystyrene cup": "foam",
    "eps": "foam",
    # ---- fishing_gear ----
    "fishing net": "fishing_gear",
    "fishing nets": "fishing_gear",
    "fishing gear": "fishing_gear",
    "net": "fishing_gear",
    "rope": "fishing_gear",
    "fishing rope": "fishing_gear",
    # ---- other ----
    "garbage": "other",
    "marine debris": "other",
    "discarded shoe": "other",
    "shoe": "other",
    "wood": "other",
    "wooden plank": "other",
    "seaweed": "other",
    "algae": "other",
}


# ---------------------------------------------------------------------------
# 历史上真实用过的提示词全集（穷举测试的靶子）
# ---------------------------------------------------------------------------

# 1) scripts/detect_marine_demo.py 的 DEFAULT_CLASSES（有 AST 对账测试守住同步）
DEMO_DEFAULT_CLASSES: tuple[str, ...] = (
    "plastic bottle",
    "plastic waste",
    "garbage",
    "fishing net",
    "foam debris",
    "marine debris",
    "discarded shoe",
)

# 2) artifacts/vision-preview/ 下四份 prompt 标定记录用过的提示词
CALIBRATION_PROMPTS: tuple[str, ...] = (
    # foam-prompt-calibration.json
    "foam",
    "styrofoam",
    "plastic pellet",
    "nurdle",
    # styrofoam-cup-calibration.json
    "styrofoam cup",
    "foam cup",
    "plastic cup",
    # styrofoam-cup-calibration-2.json
    "broken styrofoam cup",
    "damaged foam cup",
    "polystyrene cup",
    # fishing-net-calibration.json
    "net",
    "rope",
    "fishing gear",
)

# 3) 现场加词演示时会用到的词（评委随口说一个，系统应当能接住）
DEMO_LIVE_PROMPTS: tuple[str, ...] = (
    "buoy",
    "plastic bag",
    "wooden plank",
    "seaweed",
)


def normalize(prompt: str) -> str:
    """归一化提示词：去首尾空白、折叠内部连续空白、转小写。

    ★ 只做这三件事。不加复数还原、不加同义词扩展 —— 那些需要词表外知识，
    会把"映射失败"变成"猜错了但没人知道"。
    """
    return " ".join(str(prompt).split()).lower()


def map_prompt(prompt: str) -> tuple[str, str]:
    """把一条开放词汇提示词映射到契约类别。

    返回 ``(class, match_kind)``：

    - ``("foam", "exact")``            —— 直接命中映射表
    - ``("other", "fallback_other")``  —— 未命中，回落兜底类（**可观测**）

    调用方应把 ``fallback_other`` 计数暴露出来（``WorldDetector.stats()`` 就是这么做的），
    否则"提示词写错了"会表现为"这一类检测不到"，排查方向完全被带偏。
    """
    key = normalize(prompt)
    cls = PROMPT_TO_CLASS.get(key)
    if cls is None:
        return UNMAPPED_CLASS, MATCH_FALLBACK
    return cls, MATCH_EXACT


def is_mapped(prompt: str) -> bool:
    """该提示词是否在映射表里有明确归宿（而非回落 other）。"""
    return normalize(prompt) in PROMPT_TO_CLASS


def mapped_class(prompt: str) -> str:
    """只要类别、不要 match_kind 时的便捷入口。"""
    return map_prompt(prompt)[0]


def unmapped_prompts(prompts: "list[str] | tuple[str, ...]") -> list[str]:
    """一次返回这组提示词里所有"会回落 other"的项。

    给配置校验用：装箱前先跑一遍，把没归宿的提示词打出来，
    而不是等到跑了 200 帧发现某类永远是 0 才回头查。
    """
    return [p for p in prompts if not is_mapped(p)]
