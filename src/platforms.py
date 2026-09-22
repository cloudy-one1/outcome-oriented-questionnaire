"""平台适配层（v3.0）：把"这是问卷星"这件事集中到一处。

为什么要有这个模块：平台专属的选择器此前散在 5 个地方 —— 提交键在
``interactions._scripts``、翻页键与分页容器在 ``pipeline_stages.page_nav``、
题目控件在 ``pipeline_stages.page_loader``、验证组件在 ``verification``，
各自演化。想接第二个平台（或只是想知道"我们到底依赖了问卷星 DOM 的哪些地方"）
就得先把这五处翻一遍。现在它们是一张表。

**这个层刻意不做什么**（避免看着像、其实不是）：

  * 不做"多平台空壳"。题型识别与作答注入的 JS 目前仍然写死问卷星的 DOM
    约定（``#divquestionN`` / ``input[name=qN]`` / ``input[name=qN_R]`` /
    ``ul.lisort``），那不是一个常量参数能覆盖的差异。真要接第二个平台，
    要替换的是 ``detection.detect_questions`` 与 ``interactions/*`` 的脚本，
    这里给出的只是"常量清单"这一半。
  * 不做运行期可切换的当前平台单例 —— 只有一个实现时，隐式全局状态只会
    让"这次跑的到底是哪套选择器"变成要查才知道的事。

``platform_for_url`` 有一条真实用途：在批次开始前就把"这个 URL 根本不是
受支持的平台"说出来。此前它会以"探测不到题目 → 整批失败"的形态出现，
看日志的人以为是自己 DOM 适配没做好。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SurveyPlatform:
    """一个问卷平台的 DOM 约定（Python 侧能表达的那部分）。"""

    name: str
    """短名，进日志与历史记录用"""

    hosts: tuple[str, ...]
    """认这个平台的 URL host 后缀（大小写不敏感）"""

    question_control_selector: str
    """"页面已经渲染出题目"的判定选择器（等题用）"""

    submit_selectors: tuple[str, ...]
    """提交按钮候选，按优先级排列"""

    next_page_selectors: tuple[str, ...]
    """分页问卷的"下一页"按钮候选

    刻意与 ``submit_selectors`` 分开维护，而且**不含** ``#ctlNext``：
    那个 id 在某些模板里是提交键，误点等于只交了第一页。
    """

    page_wrapper_selector: str
    """分页容器候选（数量 ≥2 才承认这是分页问卷）"""

    def matches(self, url: str) -> bool:
        host = (url or "").split("://", 1)[-1].split("/", 1)[0].lower()
        return any(host == h or host.endswith("." + h) or host == h.lstrip(".")
                   for h in self.hosts)


WJX = SurveyPlatform(
    name="wjx",
    hosts=("wjx.cn", "wjx.com", "v.wjx.cn", "ks.wjx.com"),
    question_control_selector=(
        'input[type="radio"], input[type="checkbox"], select, textarea,'
        ' input[type="text"], input[type="tel"], input[type="number"]'
    ),
    submit_selectors=(
        "#divSubmit", "#submit_button", "#ctlNext",
        "button[type='submit']", "input[type='submit']",
        ".submitbtn", "#submitBtn", "#submitDiv", ".btn-submit",
        ".submitbtn.clickable", "#ctl00_ContentPlaceHolder1_ctlSubmit",
    ),
    next_page_selectors=(
        "#btnNext", "#next_btn", "#nextBtn",
        ".js-paging-next", ".nextbtn", ".btn-next",
    ),
    page_wrapper_selector=(
        '.page, .question-page, .pDiv, [class*="paging"], [data-page]'
    ),
)

PLATFORMS: tuple[SurveyPlatform, ...] = (WJX,)


def platform_for_url(url: str) -> SurveyPlatform | None:
    """按 URL 认平台；认不出来返回 ``None``（调用方决定是否提示）。"""
    for platform in PLATFORMS:
        if platform.matches(url):
            return platform
    return None


def canonical_survey_key(url: str) -> str:
    """把同一份问卷的各种 URL 形态收敛成一个键（批次匹配用，不写进页面请求）。

    为什么不能拿 URL 字符串直接当"同一份问卷"的判据：问卷星同一份问卷的答卷
    链接至少有 ``jq``（电脑端）/ ``m``（移动端）/ ``vm``、``vj``（短码分享）/
    ``hj`` 几种投放形态，微信与渠道参数还会往 query 上挂 ``kd``、``source`` 一类
    可变字段。同一份问卷经二维码解析和手输进来，字符串往往就是不一样的。
    而 ``history.find_resumable_run`` 按这个键查上次中断的批次 —— 键对不上时
    症状不是报错，是**静默不续传**：权重快照找不回来，计数从 0 开始，
    统计被拆成几行。

    收敛规则：``host（去 www. 前缀、转小写）+ ":" + 路径最后一段（去 .aspx、
    大小写原样保留）``。刻意**不**跨 host 合并 —— ``www.wjx.cn`` 与 ``v.wjx.cn``
    可能是同一问卷的不同投放渠道，但也可能是两件事，误并批次的代价是重复提交，
    比少并（只是不续传，重跑一次即可）重得多。短码大小写同理不做归一。
    """
    rest = (url or "").strip().split("://", 1)[-1]
    host, _, path_and_query = rest.partition("/")
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    path_part, _, query = path_and_query.split("#", 1)[0].partition("?")
    segments = [s for s in path_part.split("/") if s]
    ident = segments[-1] if segments else ""
    if ident.lower().endswith(".aspx"):
        ident = ident[: -len(".aspx")]
    # 结束页（``complete.aspx``）的路径段里没有问卷标识，id 挂在 query 上。
    # 不特判的话两份不同问卷的结束页会撞成同一个键 —— 而误并批次的后果是
    # 把另一份问卷的中断计数拿来续传，直接产生重复提交。
    if ident.lower() == "complete" and query:
        ident = _query_ident(query) or ident
    return f"{host}:{ident}"


def _query_ident(query: str) -> str:
    """从 query 上取问卷标识参数（``activityid`` / ``q`` / ``id``）。

    参数名换过：同类项目的历史 issue 里，同一处解析从 ``activityid`` 改成了
    ``q``。取第一个非空命中即可，不猜优先级。
    """
    for pair in query.split("&"):
        name, _, value = pair.partition("=")
        if name.strip().lower() in ("activityid", "q", "id") and value.strip():
            return value.strip()
    return ""


def unsupported_url_notice(url: str) -> str | None:
    """非受支持平台的一行提示；认得出平台时返回 ``None``。

    只提示、不拦停：问卷星的分享域名变体很多（自建镜像、短链跳转后的落地页），
    误拦的代价是把能跑的用户挡在门外，而提示的代价只是一行日志。
    """
    if platform_for_url(url) is not None:
        return None
    known = "、".join(f"{p.name}（{'/'.join(p.hosts[:2])}）" for p in PLATFORMS)
    return (
        f"[平台] URL 的域名不在已适配的范围内：{url[:80]} —— "
        f"本工具的题目探测与作答注入是按 {known} 的 DOM 约定写的，"
        "换平台大概率探测不到题目（整批会失败在『探测题目结构』这一步，而不是提交后）"
    )


__all__ = [
    "PLATFORMS",
    "SurveyPlatform",
    "WJX",
    "canonical_survey_key",
    "platform_for_url",
    "unsupported_url_notice",
]
