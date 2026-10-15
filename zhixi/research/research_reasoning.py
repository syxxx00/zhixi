"""Shared reasoning policy and scheduling hints, never a fact validator."""

import math

SOURCE_CONTEXT_POLICY = """
source_context是网页声明的日期、作者与发布者，不是独立核验。区分发布日期、数据统计期、预测期与当前日期；标题含某年份不代表文中所有数字属于该年，旧数据不能在查询或结论中自动变成现状。单个截面不能证明趋势，部分分类不能证明总体排名；报告目录或宣传摘要不等于取得整份研究，作者的评价自然注明归属。
多个source_id可能只是同一页面的不同片段，不是多个独立来源。不同网页也可能转述同一份材料；没有独立性依据不能按引用数量增加结论权重。原文确实写了某项政策、比例或因果，只证明该来源这样说；形成关键事实判断还要看其出处和适用范围。不能核实的说法可有归属地使用，不必删掉，但不能在概括时抹去归属。
prior_reading_not_evidence是匹配当前原文的历史阅读判断，可帮助检查对象、范围和替代解释，仍可能错，不能作为引用或已核实事实；与原文不符时以原文纠正，缺少记录不代表没有读过或没有依据。
分开材料的发布载体、实际讨论对象和结论适用对象。有关新闻报道、广告营销或生产工具的材料可提供相关背景，但不因包含研究关键词就变成目标产品的用户行为、技术能力或竞争原因；跨对象解释要有材料中实际建立的连接。
来源提出一个解释，只支持介绍这个解释。没有独立证据时，不把作者猜测改写成“核心障碍”“根本原因”或已确认的行业机制；这一归属须在正文、标题和概览中保持一致，不在结论后附一句免责声明来抵消错误断言。
"""

LOGIC_POLICY = SOURCE_CONTEXT_POLICY + """
先确定本段实际讨论的对象、关系与适用范围，再提出解释。并列出现不等于组织隶属，个人或特定系列特征不自动成为整体共性；相关性与因果、作者解读与作者意图也不同。
同一个人物在不同时间、活动或服装中的描述分别归属；保留原有条目边界，不能把邻近物件的属性移给另一个对象。先辨明数字表示价格、游戏属性、时间还是研究相关的观察，不用其大小替代艺术重要性或论据权重。
允许提出有依据的解释和跨实例比较，不要求所有解读都被官方逐字说过；说明支持它的具体观察、适用范围及其他可能解释。缺少材料不等于反证。
遇到省略、简称或不明词，若不影响当前解释，保留原文及必要上下文；若它会改变关键判断，记为具体补查问题。不要猜填隐藏信息，也不要因局部不懂而拒绝解释其余可理解内容。
证据权重取决于具体问题：原始档案可核实身份，实际作品可观察视觉，专业作者的分析可解释设计，评论可反映接受；不按域名或“官方”标签自动判真，也不把作者声望当作所有话题的权威。
检查应改变判断：发现前提错误时撤回或缩小依赖它的解释，不在保留错误结论的同时追加相反免责声明。原文和模型判断分开，检查记录不是新证据。
material_use_not_verified说明本片段被暂时选来回答哪个问题、用于正文还是背景，不代表全文相关或事实已核实。背景解释不能替代研究对象的属性证据；如用途标注有误，依据实际原文修正，不能机械照用标注。
修订后逐项核对被指出的关系是否真正改正，不能只换词或加“可能”而保留相同推导；同时保留未受影响的有效解释。检查意见也可能错，仍须以原文为依据。
"""

SCHEDULING_POLICY = """
weight是下一步投入的相对优先级（0到1），不是事实置信度、完成率或固定时间配额。按对原目标的重要性、当前关键缺口、预期新增解释价值来分配；已有重复材料应降低边际投入，关键矛盾或关系待核实时应提高投入。不要按篇幅、网页数量或陌生域名奖励来源。
计划是暂定解释方向，不是必须填满的章节。寻找能解释现象的实例、比较和反例，必要背景不能替代主要问题。目录、页码和视频入口的价值在于找到实际内容；仅取得元数据要如实说明。
若提供retrieval_feedback，expected_use是访问前猜测，observed_return是读取后的模型解释，并非核实事实。比较预计与实际收获来选择下一步：重复泛入口不应仅因先前高分而持续优先；关键缺口可以换词搜索、沿目录定位、补查原作品或比较已有材料。不把本批没有收获解释为原问题不重要或已经解决，也不因收获为空否定整个来源。
"""


def valid_weight(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def query_origins_for_directions(queries, directions):
    """Associate public queries without inferring semantic relations.

    One distinct direction owns the batch. Multiple directions require exact
    equality after whitespace/case normalization; unknown origins stay empty.
    """
    by_text = {}
    for direction in directions:
        normalized = ' '.join(direction.split()).casefold()
        if normalized:
            by_text.setdefault(normalized, direction)
    if len(by_text) == 1:
        direction = next(iter(by_text.values()))
        return {query: [direction] for query in queries}
    origins = {}
    for query in queries:
        normalized = ' '.join(query.split()).casefold()
        origins[query] = [by_text[normalized]] if normalized in by_text else []
    return origins


def query_origin_ids(query, plan):
    """Exact discovery origin is an allocation hint, never a source relation."""
    normalized = ' '.join((query or '').split()).casefold()
    origins = {normalized}
    for sent, directions in plan.get('query_origins', {}).items():
        # Older batches broadcast every direction to every query. Multiple
        # origins cannot establish a deterministic association; keep the query
        # usable, but do not restore that false allocation hint after restart.
        if ' '.join(sent.split()).casefold() == normalized and len(directions) == 1:
            origins.update(' '.join(direction.split()).casefold() for direction in directions)
    return [q['id'] for q in plan.get('questions', [])
            if normalized and (q.get('starting_point') or q.get('public_direction'))
            and ' '.join(q['question'].split()).casefold() in origins]


def scheduling_route(route, plan, *, partial_plan=False):
    """Enrich old provisional probes too, without certifying their contents."""
    if route.get('basis') != 'ranked_search_probe_not_evidence':
        return route
    ids = query_origin_ids(route.get('query'), plan)
    if not ids and partial_plan:
        # A reading-window projection lacks the public dispatch ledger. It
        # can retain resolved origins, but not retired/private corrections.
        public_ids = {q['id'] for q in plan.get('questions', [])
                      if q.get('starting_point') or q.get('public_direction')}
        ids = [identity for identity in route.get('question_ids', []) if identity in public_ids]
    return {**route, 'question_ids': ids,
            'attribution_basis': 'query_origin_not_verified_relation'}


def pending_reorientation(plan):
    action = plan.get('exploration_review', {}).get('pending_search', {})
    return action if action.get('state') == 'pending' and action.get('directions') else None


def route_score(route, plan):
    """Model-estimated marginal usefulness times current question allocation.

    No host penalty: many good pages on one reference site remain useful.
    Missing legacy hints fall back to the old purpose order, not a rejection.
    """
    priorities = {q['id']: q.get('research_weight', 0.5) for q in plan['questions']}
    weight = max((priorities.get(q, 0.5) for q in route.get('question_ids', [])), default=0.5)
    gain = route.get(
        'expected_usefulness', {'primary': 0.8, 'probe': 0.6, 'context': 0.4}.get(route.get('purpose'), 0.3)
    )
    return weight * gain


def allocation_focus(ids):
    """The full goal is an anchor, not a catch-all priority boost to every URL."""
    focus = set(ids)
    return focus - {'original-goal'} if focus - {'original-goal'} else focus


def choose_candidate(candidates, routes, plan):
    focus = allocation_focus(plan.get('exploration_review', {}).get('focus_ids', []))
    return max(
        candidates,
        key=lambda c: (
            routes[c['id']].get('basis') == 'explicit_user_read_request_not_evidence',
            bool(focus.intersection(routes[c['id']].get('question_ids', []))),
            route_score(routes[c['id']], plan),
            c.get('relevance', 0),
        ),
    )
