<template>
  <div class="agents">
    <!-- ======================= ① 头部：标题 + 真实 Runtime 徽标 ======================= -->
    <div class="agents__head">
      <div class="agents__head-left">
        <h2 class="agents__title">智能体运行控制台</h2>
        <p class="agents__sub">
          运行、轨迹、审批、工具与评测均来自真实 API；不展示伪造的「在线」状态。
        </p>
      </div>
      <div class="agents__head-right">
        <span class="runtime-badge" :class="`runtime-badge${runtimeBadge.cls}`">
          <i class="runtime-badge__dot"></i>
          {{ runtimeBadge.text }}
        </span>
        <button class="btn" @click="refreshAll" :disabled="refreshing">
          {{ refreshing ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- ======================= ② 全局状态横幅（failed / unavailable / degraded） ======================= -->
    <div v-if="pageState === 'failed'" class="agents__banner agents__banner--failed">
      <div class="agents__banner-title">控制台不可用（后端连接失败）</div>
      <div class="agents__banner-text">
        全部数据接口请求失败，未使用任何伪造数据兜底。请确认后端服务已启动后重试。
      </div>
      <ul class="agents__banner-list">
        <li v-for="m in failedModules" :key="m.key">
          {{ m.label }}：{{ m.error }}
        </li>
      </ul>
      <button class="btn btn--primary" @click="bootstrap">重试</button>
    </div>

    <div v-else-if="pageState === 'unavailable'" class="agents__banner agents__banner--unavailable">
      <div class="agents__banner-title">Agent 运行时不可用</div>
      <div class="agents__banner-text">
        运行时状态为 <code>unavailable</code>（后端未初始化或运行时不可用）。
        下方模块数据为只读快照，均来自真实 API。
      </div>
    </div>

    <div v-else-if="pageState === 'degraded'" class="agents__banner agents__banner--degraded">
      <div class="agents__banner-title">部分模块加载失败（降级运行）</div>
      <ul class="agents__banner-list">
        <li v-for="m in failedModules" :key="m.key">
          {{ m.label }}：{{ m.error }}
          <button class="agents__retry" @click="retryModule(m.key)">重试</button>
        </li>
      </ul>
    </div>

    <!-- ======================= ③ 运行时总览（有真实快照才渲染） ======================= -->
    <div v-if="runtime && pageState !== 'loading'" class="agents__overview">
      <div class="agents__ov-chip">
        <span class="agents__ov-k">活跃运行</span>
        <span class="agents__ov-v">{{ runtime.active_runs }}</span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">累计运行</span>
        <span class="agents__ov-v">{{ runtime.total_runs }}</span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">待审批</span>
        <span class="agents__ov-v" :class="{ 'agents__ov-v--warn': runtime.pending_approvals > 0 }">
          {{ runtime.pending_approvals }}
        </span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">已注册工具</span>
        <span class="agents__ov-v">{{ runtime.tools_registered }}</span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">模型</span>
        <span class="agents__ov-v">{{ runtime.model_available ? '可用' : '规则模式' }}</span>
      </div>
      <div class="agents__ov-chip" :title="runtime.require_approval_for_write ? 'WRITE 风险工具需要人工审批后才能执行' : 'WRITE 风险工具可直接执行（与生产策略一致）'">
        <span class="agents__ov-k">写入审批</span>
        <span class="agents__ov-v" :class="{ 'agents__ov-v--warn': runtime.require_approval_for_write }">
          {{ runtime.require_approval_for_write ? '需人工审批' : '策略放行' }}
        </span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">策略版本</span>
        <span class="agents__ov-v agents__ov-v--mono">{{ runtime.policy_version }}</span>
      </div>
      <div class="agents__ov-chip">
        <span class="agents__ov-k">运行时长</span>
        <span class="agents__ov-v agents__ov-v--mono">{{ fmtUptime(runtime.uptime_ms) }}</span>
      </div>
    </div>

    <!-- ======================= ④ 主区域网格 ======================= -->
    <div class="agents__sections">
      <!-- ---------- Runs 列表（跨两列，倒序分页 + 展开详情/回放） ---------- -->
      <section class="panel agents__runs">
        <div class="panel-title">
          <span>运行列表（按创建时间倒序）</span>
          <div class="agents__runs-tools">
            <select v-model="runsStatus" class="agents__select" @change="onFilterChange">
              <option value="">全部状态</option>
              <option v-for="(m, s) in RUN_STATUS_META" :key="s" :value="s">{{ m.label }}</option>
            </select>
            <button
              v-if="canWriteOps"
              class="btn"
              @click="showStartForm = !showStartForm"
            >
              {{ showStartForm ? '收起启动' : '启动运行' }}
            </button>
          </div>
        </div>

        <!-- 启动运行表单（POST /runs，幂等键防重复） -->
        <div v-if="showStartForm" class="agents__start">
          <div class="agents__start-row">
            <input
              v-model.trim="startForm.event_id"
              class="form-input"
              type="text"
              placeholder="事件编号 event_id（如 evt_xxxx）"
              @keyup.enter="startRun"
            />
            <input
              v-model.trim="startForm.idempotency_key"
              class="form-input"
              type="text"
              placeholder="幂等键（可选，重复提交返回既有 run）"
              @keyup.enter="startRun"
            />
            <select
              v-if="teamModeSupported"
              v-model="startForm.mode"
              class="agents__select"
              :title="startForm.mode === 'team' ? '多角色协同：事件研判 Agent 先出研判依据与结论，再交给调度执行 Agent 落地' : '单角色：既有派单管道'"
            >
              <option value="single">单智能体</option>
              <option value="team">多角色协同</option>
            </select>
            <label
              v-if="canWriteOps"
              class="agents__auto-replay"
              title="run 进入终态后自动开始逐条回放 —— 画面里就是「决策在一步步生长」"
            >
              <input v-model="autoReplay" type="checkbox" />
              <span>完成后自动回放</span>
            </label>
            <button class="btn btn--primary" @click="startRun" :disabled="starting">
              {{ starting ? '启动中…' : '启动' }}
            </button>
          </div>
          <p v-if="teamModeSupported" class="agents__start-hint">
            {{ startForm.mode === 'team'
              ? '多角色协同：研判 Agent 检索政策依据 → 给出研判结论（证据不足则拒绝自动派单）→ 调度执行 Agent 落地工单。'
              : '单智能体：读取事件 → 筛选机器人 → 生成方案 → 创建工单（评测基线同款计划）。' }}
          </p>
        </div>

        <!-- 骨架：加载态 -->
        <div v-if="runsLoading && pageState === 'loading'" class="agents__skel-list">
          <div v-for="i in 4" :key="i" class="skel" style="height: 34px"></div>
        </div>

        <!-- 空态 -->
        <div v-else-if="!runsError && !runs.length" class="empty">
          暂无运行记录 —— 可在上方「启动运行」触发，或从事件中心对 new 事件发起处置。
        </div>

        <!-- 错误态 -->
        <div v-else-if="runsError" class="agents__module-error">
          <span>运行列表加载失败：{{ runsError }}</span>
          <button class="btn" @click="loadRuns({ page: runsMeta.page })">重试</button>
        </div>

        <!-- 列表 -->
        <template v-else>
          <div class="table-scroll">
            <table class="data-table runs-table">
              <thead>
                <tr>
                  <th>运行 ID</th>
                  <th>状态</th>
                  <th class="hide-sm">触发</th>
                  <th class="hide-sm">创建时间</th>
                  <th>待审批</th>
                  <th class="agents__th-actions"></th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="r in runs"
                  :key="r.run_id"
                  class="runs-table__row"
                  :class="{ 'runs-table__row--open': expandedRunId === r.run_id }"
                  @click="openRunDetail(r.run_id)"
                >
                  <td>
                    <span class="mono">{{ r.run_id }}</span>
                    <span v-if="r.idempotent_replay" class="agents__chip agents__chip--replay" title="幂等重放：重复提交返回既有运行">幂等重放</span>
                    <span v-if="r.task_id" class="agents__chip" title="本次运行产生的工单">任务 {{ r.task_action === 'merged' ? '合并' : '' }} {{ r.task_id }}</span>
                  </td>
                  <td>
                    <span class="agents__status" :style="{ color: runStatusMeta(r.status).color, borderColor: runStatusMeta(r.status).color + '55', background: runStatusMeta(r.status).color + '1a' }">
                      {{ runStatusMeta(r.status).label }}
                    </span>
                  </td>
                  <td class="hide-sm">{{ triggerLabel(r.trigger_type) }}</td>
                  <td class="hide-sm mono dim">{{ fmtShortTime(r.created_at || r.started_at) }}</td>
                  <td>
                    <span v-if="r.pending_approval_ids?.length" class="agents__chip agents__chip--warn">
                      {{ r.pending_approval_ids.length }} 项
                    </span>
                    <span v-else class="dim">—</span>
                  </td>
                  <td class="agents__th-actions">
                    <span class="agents__expand">{{ expandedRunId === r.run_id ? '收起' : '详情' }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 分页控件 -->
          <div class="agents__pager">
            <button class="btn" :disabled="runsMeta.page <= 1" @click="goPage(runsMeta.page - 1)">上一页</button>
            <span class="agents__pager-text">
              第 {{ runsMeta.page }} / {{ runsPages }} 页 · 共 {{ runsMeta.total }} 条
            </span>
            <button class="btn" :disabled="runsMeta.page >= runsPages" @click="goPage(runsMeta.page + 1)">下一页</button>
          </div>
        </template>

        <!-- ---------- 展开的 Run 详情 + Steps 时间线（回放） ---------- -->
        <div v-if="expandedRunId" class="run-detail">
          <div v-if="detailLoading" class="agents__skel-list">
            <div v-for="i in 5" :key="i" class="skel" style="height: 22px"></div>
          </div>

          <div v-else-if="detailError" class="agents__module-error">
            <span>运行详情加载失败：{{ detailError }}</span>
            <button class="btn" @click="loadRunDetail(expandedRunId)">重试</button>
          </div>

          <template v-else-if="detailRun">
            <div class="run-detail__head">
              <div class="run-detail__title">
                <span class="mono">{{ detailRun.run_id }}</span>
                <span
                  class="agents__status"
                  :style="{ color: runStatusMeta(detailRun.status).color, borderColor: runStatusMeta(detailRun.status).color + '55', background: runStatusMeta(detailRun.status).color + '1a' }"
                >
                  {{ runStatusMeta(detailRun.status).label }}
                </span>
                <span v-if="detailRun.idempotent_replay" class="agents__chip agents__chip--replay">幂等重放</span>
                <span
                  v-if="detailRun.lesson_hits?.length"
                  class="agents__chip"
                  title="本次运行引用了经验库里的经验（摘要里写明了引用的是哪一条）"
                >引用经验 {{ detailRun.lesson_hits.length }}</span>
              </div>
              <div class="run-detail__actions">
                <button class="btn" @click="toggleReplay" :disabled="!detailSteps.length">
                  {{ replaying ? '■ 停止回放' : '▶ 回放轨迹' }}
                </button>
                <button
                  v-if="canWriteOps && canCancelRun(detailRun)"
                  class="btn btn--danger"
                  @click="onCancel(detailRun)"
                >
                  {{ cancelArmed === detailRun.run_id ? '确认取消' : '取消运行' }}
                </button>
                <button class="btn" @click="closeRunDetail">关闭</button>
              </div>
            </div>

            <div class="run-detail__meta">
              <div class="kv">
                <span class="kv__k">目标</span>
                <span class="kv__v">{{ detailRun.objective || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">触发</span>
                <span class="kv__v">{{ triggerLabel(detailRun.trigger_type) }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">策略版本</span>
                <span class="kv__v mono">{{ detailRun.policy_version || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">开始 / 结束</span>
                <span class="kv__v">{{ fmtTime(detailRun.started_at) }} → {{ fmtTime(detailRun.finished_at) }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">终止原因</span>
                <span class="kv__v">{{ detailRun.termination_reason || '—' }}</span>
              </div>
              <div class="kv" v-if="detailRun.error_code">
                <span class="kv__k">错误码</span>
                <span class="kv__v text-danger">{{ stepErrLabel(detailRun.error_code) }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">trace_id</span>
                <span class="kv__v mono">{{ detailRun.trace_id || '—' }}</span>
              </div>
            </div>

            <!-- 派单决策快照：候选排序 + 选中者 + 理由（与地图联动同源） -->
            <div v-if="detailRun.decision" class="run-decision">
              <div class="run-decision__head">
                <span class="run-decision__title">派单决策</span>
                <span class="run-decision__picked">
                  选中 {{ detailRun.decision.selected_robot_name || detailRun.decision.selected_robot_id || '—' }}
                  <template v-if="detailRun.decision.distance_m != null">
                    （{{ Math.round(Number(detailRun.decision.distance_m)) }} m）
                  </template>
                </span>
                <span class="run-decision__reason">{{ detailRun.decision.reason || '按策略择优' }}</span>
                <span v-if="detailRun.decision.recommended_action" class="agents__chip">
                  研判建议 {{ detailRun.decision.recommended_action }}
                </span>
              </div>
              <div v-if="detailRun.decision.candidates?.length" class="table-scroll">
                <table class="data-table run-decision__table">
                  <thead>
                    <tr>
                      <th>候选机器人</th>
                      <th>距离</th>
                      <th>电量</th>
                      <th>仓容占用</th>
                      <th>同类别顺路</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="c in detailRun.decision.candidates"
                      :key="c.robot_id"
                      :class="{ 'run-decision__row--picked': c.robot_id === detailRun.decision.selected_robot_id }"
                    >
                      <td class="mono">{{ c.robot_id }}</td>
                      <td>{{ c.distance_m == null ? '—' : `${Math.round(Number(c.distance_m))} m` }}</td>
                      <td>{{ c.battery == null ? '—' : `${c.battery}%` }}</td>
                      <td>{{ c.bin_usage == null ? '—' : `${(Number(c.bin_usage) * 100).toFixed(0)}%` }}</td>
                      <td>{{ c.same_category_active ? '是' : '否' }}</td>
                      <td>
                        <span
                          v-if="c.robot_id === detailRun.decision.selected_robot_id"
                          class="agents__chip agents__chip--ok"
                        >选中</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="detailRun.decision.basis?.length" class="run-decision__basis">
                <div class="run-decision__basis-title">研判依据</div>
                <ul>
                  <li v-for="(b, i) in detailRun.decision.basis" :key="i">{{ b }}</li>
                </ul>
              </div>
            </div>

            <!-- 协同角色（多角色模式才有）：谁交给了谁，一眼可见 -->
            <div v-if="detailRun.roles?.length" class="run-roles">
              <span class="run-roles__label">协同角色</span>
              <template v-for="(r, i) in detailRun.roles" :key="r">
                <span class="run-roles__item" :class="roleClass(r)">{{ r }}</span>
                <span v-if="i < detailRun.roles.length - 1" class="run-roles__arrow">→</span>
              </template>
            </div>

            <!-- 回放控制条：节奏是演示的关键，必须可控可读 -->
            <div v-if="detailSteps.length" class="replay-bar">
              <button class="btn btn--primary" @click="toggleReplay">
                {{ replaying ? '⏸ 暂停回放' : replayToggleLabel }}
              </button>
              <button v-if="replayIndex >= 0" class="btn" @click="stopReplay">■ 重置</button>
              <div class="replay-speeds" role="group" aria-label="回放速度">
                <button
                  v-for="sp in REPLAY_SPEEDS"
                  :key="sp.key"
                  class="replay-speed"
                  :class="{ 'replay-speed--on': replaySpeed === sp.key }"
                  @click="setReplaySpeed(sp.key)"
                >
                  {{ sp.label }}
                </button>
              </div>
              <span v-if="replayIndex >= 0" class="replay-progress mono">
                第 {{ Math.min(replayIndex, replayTotal) }} / {{ replayTotal }} 步
              </span>
            </div>

            <!-- 当前步骤解说：回放时把"这一步在做什么"单独读出来 -->
            <div v-if="replayIndex >= 0 && replayStep" class="replay-caption">
              <span class="replay-caption__role" :class="roleClass(replayStep.role)">
                {{ replayStep.role || '系统编排' }}
              </span>
              <span class="replay-caption__type" :style="{ color: stepTypeMeta(replayStep.step_type).color }">
                {{ stepTypeMeta(replayStep.step_type).glyph }} {{ stepTypeMeta(replayStep.step_type).label }}
              </span>
              <span class="replay-caption__text">{{ replayStep.decision_summary || '—' }}</span>
            </div>

            <!-- Steps 时间线 -->
            <div ref="stepsRef" class="steps" :class="{ 'steps--replaying': replaying }">
              <div
                v-for="st in detailSteps"
                :key="st.step_id"
                class="step"
                :class="{
                  'step--hidden': replayIndex >= 0 && st.step_no > replayIndex,
                  'step--active': replayIndex >= 0 && st.step_no === replayIndex,
                  'step--roled': !!st.role,
                }"
                :style="{ '--step-color': stepTypeMeta(st.step_type).color }"
              >
                <span class="step__no mono">{{ st.step_no }}</span>
                <span class="step__type">
                  {{ stepTypeMeta(st.step_type).glyph }} {{ stepTypeMeta(st.step_type).label }}
                </span>
                <span v-if="st.role" class="step__role" :class="roleClass(st.role)">{{ st.role }}</span>
                <span class="step__summary">
                  {{ st.decision_summary || '—' }}
                  <span v-if="st.tool_name" class="step__tool mono">
                    {{ st.tool_name }}{{ st.tool_version ? `@${st.tool_version}` : '' }}
                  </span>
                  <span v-if="st.input_hash || st.output_hash" class="step__hash mono" :title="`输入哈希 ${st.input_hash || '—'} · 输出哈希 ${st.output_hash || '—'}`">
                    哈希已审计
                  </span>
                </span>
                <span class="step__meta">
                  <span
                    class="step__status"
                    :style="{ color: stepStatusMeta(st.status).color }"
                  >
                    {{ stepStatusMeta(st.status).label }}
                  </span>
                  <span v-if="st.error_code" class="step__err" :title="stepErrLabel(st.error_code)">
                    {{ stepErrLabel(st.error_code) }}
                  </span>
                  <span class="step__lat mono hide-sm">{{ fmtMs(st.latency_ms) }}</span>
                </span>
              </div>
              <div v-if="!detailSteps.length" class="empty">该运行暂无步骤轨迹</div>
            </div>
          </template>
        </div>
      </section>

      <!-- ---------- 审批队列 ---------- -->
      <section class="panel">
        <div class="panel-title">
          <span>审批队列</span>
          <span v-if="pendingApprovals.length" class="agents__count-badge">{{ pendingApprovals.length }} 待决定</span>
        </div>

        <div v-if="approvalsLoading && pageState === 'loading'" class="agents__skel-list">
          <div v-for="i in 3" :key="i" class="skel" style="height: 46px"></div>
        </div>
        <div v-else-if="approvalsError" class="agents__module-error">
          <span>审批队列加载失败：{{ approvalsError }}</span>
          <button class="btn" @click="loadApprovals()">重试</button>
        </div>
        <div v-else-if="!approvals.length" class="empty">暂无审批记录</div>
        <div v-else class="approvals">
          <!-- 待决定项 -->
          <div v-for="a in pendingApprovals" :key="a.approval_id" class="approval approval--pending">
            <div class="approval__head">
              <span class="mono">{{ a.approval_id }}</span>
              <span class="agents__chip agents__chip--warn">待决定</span>
              <span class="approval__risk" :style="{ color: riskMeta(a.risk_level).color, borderColor: riskMeta(a.risk_level).color + '55', background: riskMeta(a.risk_level).color + '1a' }">
                {{ riskMeta(a.risk_level).label }}
              </span>
            </div>
            <div class="approval__body">
              <div class="approval__action">{{ a.requested_action }}</div>
              <div class="approval__meta">
                <span>run: <span class="mono">{{ a.run_id }}</span></span>
                <span>请求人: {{ a.requested_by }}</span>
                <span>请求时间: {{ fmtShortTime(a.requested_at) }}</span>
              </div>
            </div>
            <div class="approval__actions">
              <button
                class="btn btn--approve"
                :disabled="!canApprove || deciding === a.approval_id"
                :title="!canApprove ? '需要 admin 或 approver 角色（后端强制校验）' : ''"
                @click="decide(a, 'approved')"
              >
                {{ deciding === a.approval_id ? '处理中…' : '同意' }}
              </button>
              <button
                class="btn btn--reject"
                :disabled="!canApprove || deciding === a.approval_id"
                :title="!canApprove ? '需要 admin 或 approver 角色（后端强制校验）' : ''"
                @click="decide(a, 'rejected')"
              >
                拒绝
              </button>
              <span v-if="!canApprove" class="approval__hint">无审批权限（需 admin/approver）</span>
            </div>
          </div>

          <!-- 已决策记录 -->
          <div v-for="a in decidedApprovals" :key="a.approval_id" class="approval approval--done">
            <div class="approval__head">
              <span class="mono dim">{{ a.approval_id }}</span>
              <span class="agents__chip" :class="a.decision === 'approved' ? 'agents__chip--ok' : 'agents__chip--no'">
                {{ a.decision === 'approved' ? '已同意' : '已拒绝' }}
              </span>
            </div>
            <div class="approval__meta">
              <span>run: <span class="mono">{{ a.run_id }}</span></span>
              <span>决定人: {{ a.decided_by || '—' }}</span>
              <span>理由: {{ a.reason || '—' }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ---------- 工具目录 ---------- -->
      <section class="panel">
        <div class="panel-title">
          <span>工具目录</span>
          <span v-if="tools.length" class="agents__count-badge">{{ tools.length }} 个</span>
        </div>

        <div v-if="toolsLoading && pageState === 'loading'" class="agents__skel-list">
          <div v-for="i in 3" :key="i" class="skel" style="height: 40px"></div>
        </div>
        <div v-else-if="toolsError" class="agents__module-error">
          <span>工具目录加载失败：{{ toolsError }}</span>
          <button class="btn" @click="loadTools()">重试</button>
        </div>
        <div v-else-if="!tools.length" class="empty">工具目录为空</div>
        <div v-else class="table-scroll">
          <table class="data-table tools-table">
            <thead>
              <tr>
                <th>工具</th>
                <th>版本</th>
                <th>风险</th>
                <th>幂等</th>
                <th>超时</th>
                <th>允许角色</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="t in tools" :key="t.name">
                <tr>
                  <td>
                    <div class="tools__name mono">{{ t.name }}</div>
                    <div class="tools__desc">{{ t.description }}</div>
                    <button class="tools__schema-toggle" @click="toggleSchema(t.name)">
                      {{ expandedSchemas[t.name] ? '收起 Schema' : '查看 Schema' }}
                    </button>
                  </td>
                  <td class="mono dim">{{ t.version }}</td>
                  <td>
                    <span class="agents__status" :style="{ color: riskMeta(t.risk_level).color, borderColor: riskMeta(t.risk_level).color + '55', background: riskMeta(t.risk_level).color + '1a' }">
                      {{ riskMeta(t.risk_level).label }}
                    </span>
                  </td>
                  <td>{{ t.idempotent ? '是' : '否' }}</td>
                  <td class="mono dim">{{ fmtMs(t.timeout_ms) }}</td>
                  <td class="dim">{{ (t.allowed_roles || []).join(' / ') }}</td>
                </tr>
                <tr v-if="expandedSchemas[t.name]">
                  <td colspan="6">
                    <div class="tools__schema">
                      <div>
                        <div class="tools__schema-title">输入 Schema</div>
                        <pre>{{ jsonPretty(t.input_schema) }}</pre>
                      </div>
                      <div>
                        <div class="tools__schema-title">输出 Schema</div>
                        <pre>{{ jsonPretty(t.output_schema) }}</pre>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ---------- 评测摘要（WP-05/WP-12 v2 产物契约） ---------- -->
      <section class="panel">
        <div class="panel-title"><span>Agent 评测摘要</span></div>

        <div v-if="evalsLoading && pageState === 'loading'" class="agents__skel-list">
          <div v-for="i in 3" :key="i" class="skel" style="height: 40px"></div>
        </div>
        <div v-else-if="evalsError" class="agents__module-error">
          <span>评测数据加载失败：{{ evalsError }}</span>
          <button class="btn" @click="loadEvals()">重试</button>
        </div>
        <div v-else-if="evalsEmpty" class="agents__eval-empty">
          <div class="agents__eval-empty-title">评测数据不可用</div>
          <div class="agents__eval-empty-text">
            后端返回错误码 6008（评测产物缺失/损坏/不符合契约），未伪造任何指标。
            请先运行固定场景评测（WP-05/WP-12）生成
            <code>artifacts/agent_evals/latest_v2.json</code>。
          </div>
        </div>
        <template v-else-if="evals">
          <div class="eval">
            <div class="eval__head">
              <span class="eval__tag">{{ evals.evidence_level || '—' }} 证据</span>
              <span class="eval__tag">schema {{ evals.schema_version || '—' }}</span>
              <span class="eval__tag">{{ evals.report_type || '—' }}</span>
              <span class="eval__date">{{ fmtTime(evals.date) }}</span>
            </div>

            <div class="eval__score">
              <div class="eval__score-num">
                {{ passedCount }}<span class="eval__score-den"> / {{ evals.sample_size ?? 0 }}</span>
              </div>
              <div class="eval__score-label">
                场景通过（执行 {{ evals.sample_size ?? 0 }} · 跳过 {{ evals.skipped_count ?? 0 }}）
              </div>
            </div>

            <!-- 叙事抬头：把"多少场景、注入了什么、结果如何"一句话讲清楚 -->
            <p v-if="evalNarrative" class="eval__narrative">{{ evalNarrative }}</p>

            <!-- 质量雷达 + 四维得分 -->
            <div class="eval__radar">
              <ChartPanel bare :option="evalRadarOption" :height="330" />
              <div class="eval__radar-side">
                <div class="eval__radar-note">{{ EVAL_RADAR_NOTE }}</div>
                <div class="eval__radar-dims">
                  <div v-for="d in evalRadarDims" :key="d.name" class="eval__radar-dim">
                    <span class="eval__radar-dim-name">{{ d.name }}</span>
                    <span class="eval__radar-dim-bar">
                      <i :style="{ width: `${Math.round(d.score * 100)}%` }"></i>
                    </span>
                    <span class="eval__radar-dim-score mono">{{ Math.round(d.score * 100) }}%</span>
                  </div>
                </div>
                <div class="eval__radar-tags">
                  <span v-for="[key, label] in EVAL_OFF_RADAR_METRICS" :key="key" class="eval__tag">
                    {{ label }}：{{ fmtMetric(key, evals.metrics?.[key]) }}
                  </span>
                </div>
              </div>
            </div>

            <div class="eval__metrics">
              <div v-for="[key, label] in EVAL_METRIC_LABELS" :key="key" class="eval__metric">
                <span class="eval__metric-k">{{ label }}</span>
                <span class="eval__metric-v">{{ fmtMetric(key, evals.metrics?.[key]) }}</span>
              </div>
            </div>

            <button class="tools__schema-toggle" @click="showEvalDetail = !showEvalDetail">
              {{ showEvalDetail ? '收起产物详情' : '查看评测产物详情' }}
            </button>
            <div v-if="showEvalDetail" class="eval__detail">
              <div class="kv">
                <span class="kv__k">命令</span>
                <span class="kv__v mono">{{ evals.command || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">代码版本</span>
                <span class="kv__v mono">{{ evals.code_version?.value || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">代码指纹</span>
                <span class="kv__v mono">{{ evals.code_version?.fingerprint || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">配置哈希</span>
                <span class="kv__v mono">{{ evals.config?.hash || '—' }}</span>
              </div>
              <div class="kv">
                <span class="kv__k">环境</span>
                <span class="kv__v">
                  网络 {{ boolLabel(evals.environment?.network_access) }} · 模型 {{ boolLabel(evals.environment?.model_access) }} · 设备 {{ boolLabel(evals.environment?.device_access) }} · {{ evals.environment?.runtime_backend || '—' }} / {{ evals.environment?.persistent_backend || '—' }}
                </span>
              </div>
              <div class="eval__denoms">
                <div class="tools__schema-title">分母台账（denominators）</div>
                <pre>{{ jsonPretty(evals.metrics?.denominators || {}) }}</pre>
              </div>
              <div class="eval__denoms">
                <div class="tools__schema-title">指标口径（definitions）</div>
                <pre>{{ jsonPretty(evals.metrics?.definitions || {}) }}</pre>
              </div>

              <!-- 故障注入回放：按"注入了什么、要求系统怎样"分组讲 -->
              <div class="eval__scenarios">
                <div class="tools__schema-title">场景回放（{{ evals.scenarios?.length || 0 }}）</div>
                <div v-for="grp in evalScenarioGroups" :key="grp.key" class="eval__group">
                  <div class="eval__group-head">
                    <span class="eval__group-title">{{ grp.label }}</span>
                    <span class="agents__chip" :class="grp.passed === grp.items.length ? 'agents__chip--ok' : 'agents__chip--no'">
                      {{ grp.passed }} / {{ grp.items.length }} 通过
                    </span>
                    <span class="eval__group-hint">{{ grp.hint }}</span>
                  </div>
                  <div v-for="sc in grp.items" :key="sc.id" class="eval__scenario">
                    <div class="eval__scenario-head">
                      <span class="mono">{{ sc.id }}</span>
                      <span class="eval__scenario-name">{{ sc.name || '—' }}</span>
                      <span class="agents__chip" :class="sc.passed ? 'agents__chip--ok' : 'agents__chip--no'">
                        {{ sc.passed ? '通过' : '未通过' }}
                      </span>
                    </div>
                    <div class="eval__scenario-body">
                      <span>期望：{{ sc.expected?.status || '—' }}{{ sc.expected?.error_code ? ` / ${sc.expected.error_code}` : '' }}</span>
                      <span>实际：{{ sc.actual?.status || '—' }}{{ sc.actual?.error_code ? ` / ${sc.actual.error_code}` : '' }}</span>
                      <span v-if="sc.actual?.termination_reason">终止：{{ sc.actual.termination_reason }}</span>
                      <span>步骤：{{ sc.actual?.step_count ?? '—' }}（{{ (sc.actual?.step_types || []).join(' → ') }}）</span>
                      <span v-if="sc.actual?.decision_latency_ms != null">决策耗时：{{ fmtMs(sc.actual.decision_latency_ms) }}</span>
                    </div>
                    <div v-if="sc.description" class="eval__scenario-desc">{{ sc.description }}</div>
                  </div>
                  <div v-if="!grp.items.length" class="empty">该组无场景</div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </section>

      <!-- ---------- 跨 run 经验库（lessons：确定性「可进化」闭环） ---------- -->
      <section class="panel">
        <div class="panel-title">
          <span>跨 run 经验库</span>
          <span v-if="lessons.length" class="agents__count-badge">{{ lessons.length }} 条</span>
        </div>

        <div v-if="!lessons.length" class="empty lessons__empty">
          暂无经验。经验由已完成的运行按确定性规则复盘提炼（成功/失败各有一套规则），
          命中适用条件后写进后续运行的规划摘要 —— 轨迹里能看到「本次引用了哪条经验」。
          进程内累积，后端重启后清零。
        </div>
        <div v-else class="lessons">
          <article v-for="l in lessons" :key="l.lesson_id" class="lesson">
            <div class="lesson__head">
              <span class="lesson__scope mono">{{ l.scope_id }}</span>
              <span class="lesson__kind mono">{{ l.kind }}</span>
              <span class="lesson__conf" :title="`置信度由证据强度 + 命中次数单调上调，封顶 95%`">
                {{ (Number(l.confidence) * 100).toFixed(0) }}%
              </span>
              <span class="lesson__hits">
                引用 {{ l.hit_count }} 次 · 确认 {{ l.confirm_count }} 次
              </span>
            </div>
            <div class="lesson__cond">适用条件：{{ l.condition }}</div>
            <div class="lesson__guide">{{ l.guidance }}</div>
          </article>
        </div>
      </section>

      <!-- ---------- 能力定义卡片（真实状态徽标） ---------- -->
      <section class="panel">
        <div class="panel-title">
          <span>能力定义</span>
          <span v-if="agents.length" class="agents__count-badge">{{ agents.length }} 项</span>
        </div>

        <div v-if="agentsLoading && pageState === 'loading'" class="agents__skel-list">
          <div class="skel" style="height: 90px"></div>
          <div class="skel" style="height: 90px"></div>
        </div>
        <div v-else-if="capabilitiesError" class="agents__module-error">
          <span>能力定义加载失败：{{ capabilitiesError }}</span>
          <button class="btn" @click="loadCapabilities()">重试</button>
        </div>
        <div v-else-if="!agents.length" class="empty">暂无能力定义</div>
        <div v-else class="agents__grid-cards">
          <article v-for="(a, i) in agents" :key="a.id" class="agent-card" :style="{ animationDelay: `${i * 50}ms` }">
            <div class="agent-card__top">
              <div class="agent-card__badge">{{ a.glyph }}</div>
              <div class="agent-card__id">
                <div class="agent-card__name">{{ a.name }}</div>
                <div class="agent-card__codename">{{ a.codename }}</div>
              </div>
              <span class="agent-card__status" :class="`agent-card__status${agentStatusMeta(a.status).cls}`">
                <i class="agent-card__dot"></i>
                {{ agentStatusMeta(a.status).label }}
              </span>
            </div>
            <div class="agent-card__role">{{ a.role }}</div>
            <p class="agent-card__desc">{{ a.description }}</p>
            <div class="agent-card__caps">
              <span v-for="c in a.capabilities" :key="c" class="agent-card__cap">{{ c }}</span>
            </div>
          </article>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
/**
 * 智能体运行控制台（WP-04）。
 *
 * 数据全部来自冻结契约 /api/v1/agents/* 与 /api/v1/ai/agents：
 *   - 不写死「N 个智能体在线」，状态徽标来自 GET /runtime/status 的真实快照；
 *   - 后端不可达时显示真实错误（failed / degraded），绝不 fallback 到伪造数据；
 *   - 五种状态：loading / empty / unavailable / degraded / failed 逐级区分。
 */
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { aiApi, agentsApi } from '@/api'
import ChartPanel from '@/components/ChartPanel.vue'
import { useRealtimeStore } from '@/stores/realtime'
import { canWrite, getUser } from '@/utils/auth'
import { fmtTime, fmtShortTime } from '@/utils/format'

// ---------- query 直达支持（router props：?run=<run_id> 自动展开，?page=<n> 直达页码） ----------
const props = defineProps({
  runId: { type: String, default: '' },
  page: { type: Number, default: 1 },
})

const store = useRealtimeStore()
const route = useRoute()
const router = useRouter()

// ======================= 冻结契约的标签映射（Harness 手册 3.1-3.3、3.8） =======================
const RUN_STATUS_META = {
  created: { label: '已创建', color: '#8e8e93' },
  planning: { label: '规划中', color: '#007aff' },
  waiting_policy: { label: '等待策略', color: '#ff9500' },
  waiting_approval: { label: '等待审批', color: '#ff9500' },
  executing: { label: '执行中', color: '#007aff' },
  observing: { label: '观察中', color: '#007aff' },
  verifying: { label: '验证中', color: '#5856d6' },
  succeeded: { label: '成功', color: '#34c759' },
  failed: { label: '失败', color: '#ff3b30' },
  cancelled: { label: '已取消', color: '#8e8e93' },
  expired: { label: '已过期', color: '#ff3b30' },
}

const STEP_TYPE_META = {
  plan: { label: '计划', glyph: '◈', color: '#007aff' },
  policy: { label: '策略校验', glyph: '§', color: '#5856d6' },
  approval_request: { label: '审批请求', glyph: '◎', color: '#ff9500' },
  tool_call: { label: '工具调用', glyph: '⚙', color: '#007aff' },
  observation: { label: '观察', glyph: '◉', color: '#007aff' },
  verification: { label: '验证', glyph: '✓', color: '#34c759' },
  replan: { label: '重规划', glyph: '↻', color: '#ff9500' },
  terminal: { label: '终止', glyph: '■', color: '#ff3b30' },
}

const STEP_STATUS_META = {
  ok: { label: '成功', color: '#34c759' },
  pending: { label: '挂起', color: '#ff9500' },
  failed: { label: '失败', color: '#ff3b30' },
}

const RISK_META = {
  read_only: { label: '只读', color: '#34c759' },
  write: { label: '写入', color: '#ff9500' },
  device_command: { label: '设备指令', color: '#ff3b30' },
  sensitive: { label: '敏感', color: '#5856d6' },
}

const TRIGGER_LABELS = { event: '事件触发', manual: '人工触发', schedule: '定时触发' }

// 步骤/运行的结构化错误码（手册 3.3）
const STEP_ERROR_LABELS = {
  no_robot_available: '无可用机器人',
  tool_timeout: '工具超时',
  tool_failed: '工具执行失败',
  policy_denied: '策略拒绝',
  approval_rejected: '审批拒绝',
  approval_timeout: '审批超时',
  task_conflict: '任务冲突',
  invalid_tool_input: '工具输入非法',
  invalid_tool_output: '工具输出非法',
  max_steps_exceeded: '超过最大步数',
  run_expired: '运行过期',
  internal_error: '内部错误',
}

// Agent API 业务错误码（手册 3.8：6001-6009）
const AGENT_API_ERRORS = {
  6001: '运行不存在',
  6002: '非法状态迁移（run 已进入终态，无法推进）',
  6003: '审批不可决定（不存在 / 已决策 / 决策值非法）',
  6004: '运行不可取消（已进入终态）',
  6005: '触发事件不存在',
  6006: '事件状态不允许派单',
  6007: '业务冲突（事件已存在活跃工单）',
  6008: '评测产物缺失或不可读',
  6009: '运行时不可用',
}

const EVAL_METRIC_LABELS = [
  ['success_rate', '任务成功率'],
  ['policy_violation_rate', '策略违规率'],
  ['tool_correct_rate', '工具调用正确率'],
  ['invalid_loop_rate', '无效循环率'],
  ['recovery_success_rate', '恢复成功率'],
  ['p95_decision_latency_ms', 'P95 决策耗时'],
  ['restart_recovery_rate', '重启恢复率'],
  ['idempotency_conflict_rate', '幂等冲突检出率'],
  ['model_fallback_rate', '模型回退率'],
  ['model_schema_rejection_rate', '模型 Schema 拒绝率'],
  ['trace_replay_match_rate', '轨迹回放匹配率'],
  ['approval_handoff_success_rate', '审批交接成功率'],
  ['device_fault_recovery_rate', '设备故障恢复率'],
  ['business_success_rate', '业务成功率'],
]

// ======================= 评测叙事的两种"讲法" =======================
// 质量雷达：把方向明确的 11 项指标一次看完，按 4 个维度分组。
//
// 两个刻意取舍（不这么做就是在编数据）：
//  1) "越低越好"的指标（违规率 / 无效循环率）先取 1-x 再画 ——
//     否则"0 违规"会落在雷达中心，屏幕上看上去像做得最差；
//  2) 不进雷达的三项各有原因：
//       · p95_decision_latency_ms 不是比率，归一化没有意义；
//       · model_fallback_rate 高/低都不直接等于好坏（规则兜底本身是设计）；
//       · model_schema_rejection_rate 同理（拒绝率高低都取决于模型输出质量）。
//     它们仍然在下方指标卡里照常展示，不藏。
const EVAL_RADAR_NOTE =
  '雷达只画方向明确（越高越好 / 越低越好）的 11 项；违规率与无效循环率已取 1-x 反向归一。'
const EVAL_RADAR_METRICS = [
  { key: 'success_rate', label: '任务成功率', group: '完成度' },
  { key: 'business_success_rate', label: '业务成功率', group: '完成度' },
  { key: 'tool_correct_rate', label: '工具正确率', group: '完成度' },
  { key: 'policy_violation_rate', label: '无策略违规', group: '安全性', inverted: true },
  { key: 'invalid_loop_rate', label: '无无效循环', group: '安全性', inverted: true },
  { key: 'recovery_success_rate', label: '恢复成功率', group: '韧性' },
  { key: 'restart_recovery_rate', label: '重启恢复率', group: '韧性' },
  { key: 'device_fault_recovery_rate', label: '设备故障恢复率', group: '韧性' },
  { key: 'trace_replay_match_rate', label: '轨迹回放一致', group: '可追溯' },
  { key: 'approval_handoff_success_rate', label: '审批交接成功', group: '可追溯' },
  { key: 'idempotency_conflict_rate', label: '幂等冲突检出', group: '可追溯' },
]
const EVAL_RADAR_GROUPS = ['完成度', '安全性', '韧性', '可追溯']
const EVAL_OFF_RADAR_METRICS = [
  ['p95_decision_latency_ms', 'P95 决策耗时'],
  ['model_fallback_rate', '模型回退率'],
  ['model_schema_rejection_rate', '模型输出拦截率'],
]

// 场景分组：按"注入了什么、要求系统怎样"讲，
// 比平铺 27 条更容易让评委听懂这套评测到底测了什么。
//
// 分组判据是**客观、可核对**的：
//   fault    ← 期望里带 error_code（即期望它安全停下）—— 直接读产物，不会算错
//   disturb  ← 期望成功，且 id 命中"额外机制介入"关键字（见下面的正则）
//   baseline ← 期望成功，且没有任何额外机制介入
// 三组之和恒等于 sample_size。
const EVAL_SCENARIO_GROUPS = [
  {
    key: 'fault',
    label: '故障 / 门禁拦截 · 期望安全终止',
    hint: '故意注入异常、或让研判门禁否决，要求系统给出结构化错误码并安全停下，不产生越权副作用。',
  },
  {
    key: 'disturb',
    label: '干扰注入 · 期望仍走完闭环',
    hint: '注入模型异常 / 重启 / 并发 / 设备故障，或切到多角色协同、跨 run 经验等额外机制，要求系统照常把派单做完。',
  },
  {
    key: 'baseline',
    label: '基线闭环（无额外机制介入）',
    hint: '没有任何注入与额外机制，验证常规派单闭环本身。',
  },
]
// ★ 这份正则必须跟着固定场景集一起更新。
//   漏了关键字不会让数字出错（三组之和恒等于场景总数），但会把新场景误挂到
//   "基线闭环"上，分组标签就失真了。当前覆盖的是 27 个场景里全部"额外机制介入"的机制：
//     模型适配 / 持久化重启 / 幂等并发 / 旧版本冲突 / 轨迹回放 / 审批交接 /
//     设备故障注入 / 重规划恢复 / 多角色研判（assessor）/ 跨 run 经验（lessons）
//   权威来源：backend/tests/agent_evals/scenarios.py 的 SCENARIOS（第四波已扩到 27）。
const EVAL_DISTURBANCE_RE =
  /model|restart|recovery|idempotent|concurrent|stale|trace_|handoff|fault|replan|assessor|lessons/

function evalGroupKeyOf(scenario) {
  if (scenario?.expected?.error_code) return 'fault'
  if (EVAL_DISTURBANCE_RE.test(String(scenario?.id || ''))) return 'disturb'
  return 'baseline'
}

const RUNNING_STATES = new Set([
  'created', 'planning', 'waiting_policy', 'waiting_approval',
  'executing', 'observing', 'verifying',
])

const MODULE_DEFS = [
  { key: 'runtime', label: '运行时状态' },
  { key: 'capabilities', label: '能力定义' },
  { key: 'runs', label: '运行列表' },
  { key: 'approvals', label: '审批队列' },
  { key: 'tools', label: '工具目录' },
  { key: 'evals', label: '评测摘要' },
]

// ======================= 状态 =======================
const booting = ref(true)                       // 首次整页加载
const refreshing = ref(false)

const runtime = ref(null)
const runtimeLoading = ref(false)

// 计划变体由后端 /runtime/status 给出（不写死）：后端只注册了 single 时，
// 前端就不会显示一个点了会报错的「多角色协同」选项。
const teamModeSupported = computed(() =>
  Array.isArray(runtime.value?.plan_variants)
    ? runtime.value.plan_variants.includes('team')
    : false,
)

const agents = ref([])
const agentsLoading = ref(false)

const runs = ref([])
const runsMeta = reactive({ total: 0, page: 1, page_size: 10 })
const runsLoading = ref(false)
const runsStatus = ref('')
const showStartForm = ref(false)
const startForm = reactive({ event_id: '', idempotency_key: '', mode: 'single' })
const starting = ref(false)

const approvals = ref([])
const approvalsLoading = ref(false)
const deciding = ref('')

const tools = ref([])
const toolsLoading = ref(false)
const expandedSchemas = reactive({})

const evals = ref(null)
const evalsLoading = ref(false)
const evalsEmpty = ref(false)
const showEvalDetail = ref(false)

const expandedRunId = ref('')
const detailRun = ref(null)
const detailSteps = ref([])
const detailLoading = ref(false)
const detailError = ref('')
const cancelArmed = ref('')

// 各模块错误（页面级状态机的输入）
const modErrors = reactive({ runtime: '', capabilities: '', runs: '', approvals: '', tools: '', evals: '' })

// 模板直接引用的模块错误文案
const runsError = computed(() => modErrors.runs)
const approvalsError = computed(() => modErrors.approvals)
const toolsError = computed(() => modErrors.tools)
const capabilitiesError = computed(() => modErrors.capabilities)

// ======================= 权限（仅 UI 显隐，后端是最终裁决） =======================
const canWriteOps = canWrite()
const canApprove = computed(() => {
  const role = getUser()?.role
  return role === 'admin' || role === 'approver'
})

// ======================= 页面级状态机：loading / failed / unavailable / degraded / ready =======================
const failedModules = computed(() =>
  MODULE_DEFS.filter((m) => modErrors[m.key]).map((m) => ({ key: m.key, label: m.label, error: modErrors[m.key] })),
)

const pageState = computed(() => {
  if (booting.value) return 'loading'
  const failed = failedModules.value
  // 全部模块失败 = 后端整体不可达 → failed（显示真实错误 + 重试）
  if (failed.length >= MODULE_DEFS.length) return 'failed'
  // 部分失败 → degraded（其余模块正常展示）
  if (failed.length > 0) return 'degraded'
  // 后端可达但运行时未初始化 → unavailable
  if (runtime.value?.state === 'unavailable') return 'unavailable'
  return 'ready'
})

// ======================= 运行时徽标 =======================
const runtimeBadge = computed(() => {
  if (booting.value || runtimeLoading.value) return { cls: '--loading', text: '加载中' }
  if (modErrors.runtime) return { cls: '--error', text: '状态未知' }
  const map = {
    idle: { cls: '--idle', text: '空闲 idle' },
    running: { cls: '--running', text: '运行中 running' },
    unavailable: { cls: '--unavailable', text: '不可用 unavailable' },
  }
  const hit = map[runtime.value?.state]
  return hit || { cls: '--unknown', text: runtime.value?.state || '未知' }
})

// ======================= 加载器 =======================
function agentErrText(err) {
  if (!err) return '未知错误'
  const msg = err.message || String(err)
  if (err.code && AGENT_API_ERRORS[err.code]) return `[${err.code}] ${AGENT_API_ERRORS[err.code]}（${msg}）`
  return msg
}

async function loadRuntime({ silent = false } = {}) {
  if (!silent) runtimeLoading.value = true
  modErrors.runtime = ''
  try {
    runtime.value = await agentsApi.runtimeStatus()
  } catch (err) {
    runtime.value = null
    modErrors.runtime = agentErrText(err)
  } finally {
    if (!silent) runtimeLoading.value = false
  }
}

async function loadCapabilities({ silent = false } = {}) {
  if (!silent) agentsLoading.value = true
  modErrors.capabilities = ''
  try {
    agents.value = (await aiApi.agents()) || []
  } catch (err) {
    agents.value = []
    modErrors.capabilities = agentErrText(err)
  } finally {
    if (!silent) agentsLoading.value = false
  }
}

async function loadRuns({ page = runsMeta.page, silent = false } = {}) {
  if (!silent) runsLoading.value = true
  modErrors.runs = ''
  try {
    const res = await agentsApi.listRuns({
      page,
      page_size: runsMeta.page_size,
      status: runsStatus.value || undefined,
    })
    runs.value = res?.items || []
    runsMeta.total = res?.meta?.total ?? runs.value.length
    runsMeta.page = res?.meta?.page ?? page
    runsMeta.page_size = res?.meta?.page_size ?? runsMeta.page_size
  } catch (err) {
    runs.value = []
    modErrors.runs = agentErrText(err)
  } finally {
    if (!silent) runsLoading.value = false
  }
}

async function loadApprovals({ silent = false } = {}) {
  if (!silent) approvalsLoading.value = true
  modErrors.approvals = ''
  try {
    approvals.value = (await agentsApi.approvals()) || []
  } catch (err) {
    approvals.value = []
    modErrors.approvals = agentErrText(err)
  } finally {
    if (!silent) approvalsLoading.value = false
  }
}

async function loadTools({ silent = false } = {}) {
  if (!silent) toolsLoading.value = true
  modErrors.tools = ''
  try {
    tools.value = (await agentsApi.tools()) || []
  } catch (err) {
    tools.value = []
    modErrors.tools = agentErrText(err)
  } finally {
    if (!silent) toolsLoading.value = false
  }
}

async function loadEvals({ silent = false } = {}) {
  if (!silent) evalsLoading.value = true
  modErrors.evals = ''
  evalsEmpty.value = false
  try {
    evals.value = await agentsApi.latestEval()
  } catch (err) {
    evals.value = null
    if (err.code === 6008) {
      // 评测产物缺失 → 空态（不是错误，也不是伪造指标）
      evalsEmpty.value = true
    } else {
      modErrors.evals = agentErrText(err)
    }
  } finally {
    if (!silent) evalsLoading.value = false
  }
}

// 首屏 + 整页重试
async function bootstrap() {
  booting.value = true
  await Promise.all([
    loadRuntime(),
    loadCapabilities(),
    loadRuns({ page: props.page > 0 ? props.page : 1 }),
    loadApprovals(),
    loadTools(),
    loadEvals(),
  ])
  booting.value = false
  if (props.runId) openRunDetail(props.runId)
}

// 静默全量刷新（头部「刷新」按钮）
async function refreshAll() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.all([
      loadRuntime({ silent: true }),
      loadCapabilities({ silent: true }),
      loadRuns({ page: runsMeta.page, silent: true }),
      loadApprovals({ silent: true }),
      loadTools({ silent: true }),
      loadEvals({ silent: true }),
    ])
  } finally {
    refreshing.value = false
  }
}

function retryModule(key) {
  const map = {
    runtime: () => loadRuntime(),
    capabilities: () => loadCapabilities(),
    runs: () => loadRuns({ page: runsMeta.page }),
    approvals: () => loadApprovals(),
    tools: () => loadTools(),
    evals: () => loadEvals(),
  }
  map[key]?.()
}

// ======================= Runs 分页 / 筛选 =======================
const runsPages = computed(() => Math.max(1, Math.ceil(runsMeta.total / Math.max(1, runsMeta.page_size))))

function onFilterChange() {
  runsMeta.page = 1
  loadRuns({ page: 1 })
  syncQuery({ page: 1 })
}

function goPage(n) {
  if (n < 1 || n > runsPages.value || n === runsMeta.page) return
  runsMeta.page = n
  loadRuns({ page: n })
  syncQuery({ page: n })
}

// ======================= Run 详情 + Steps 回放 =======================
let detailRequestToken = 0

async function loadRunDetail(runId) {
  const requestToken = ++detailRequestToken
  stopReplay()
  expandedRunId.value = runId
  syncQuery({ run: runId })
  detailLoading.value = true
  detailError.value = ''
  detailRun.value = null
  detailSteps.value = []
  try {
    const [run, steps] = await Promise.all([
      agentsApi.runDetail(runId),
      agentsApi.runSteps(runId),
    ])
    if (requestToken !== detailRequestToken) return
    detailRun.value = run
    detailSteps.value = steps || []
  } catch (err) {
    if (requestToken !== detailRequestToken) return
    detailError.value = agentErrText(err)
  } finally {
    if (requestToken === detailRequestToken) detailLoading.value = false
  }
}

async function openRunDetail(runId) {
  if (expandedRunId.value === runId) {
    closeRunDetail()
    return
  }
  await loadRunDetail(runId)
}

function closeRunDetail() {
  detailRequestToken += 1
  stopReplay()
  expandedRunId.value = ''
  detailRun.value = null
  detailSteps.value = []
  detailError.value = ''
  cancelArmed.value = ''
  clearQuery('run')
}

// 轮询时刷新打开的详情（只刷非终态 run）
async function refreshDetail(runId) {
  if (!detailRun.value) return
  // 与 loadRunDetail 共用同一令牌：轮询响应若晚于「打开新详情 / 关闭详情」落地，必须丢弃
  const requestToken = detailRequestToken
  try {
    const [run, steps] = await Promise.all([
      agentsApi.runDetail(runId),
      agentsApi.runSteps(runId),
    ])
    if (requestToken !== detailRequestToken) return
    detailRun.value = run
    detailSteps.value = steps || []
  } catch {
    /* 轮询失败保留旧数据，不打断页面 */
  }
}

// ======================= 轨迹回放（演示主镜头） =======================
// 为什么必须能"慢下来"：规则模式下一次派单是毫秒级的，POST /runs 到终态
// 屏幕上几乎一眨眼就结束了。评委想看的恰恰是"它在一步步想、一步步做"，
// 所以回放默认 1.5 秒一步，并且把"当前这一步是谁在做、在做什么"单独读出来。
const REPLAY_SPEEDS = [
  { key: 'slow', label: '慢速 2s', ms: 2000 },
  { key: 'normal', label: '标准 1.5s', ms: 1500 },
  { key: 'fast', label: '快 0.8s', ms: 800 },
]
let replayTimer = null
const replaying = ref(false)
// 已点亮的最大 step_no；-1 表示"未进入回放，全部可见"
const replayIndex = ref(-1)
const replaySpeed = ref('normal')
const stepsRef = ref(null)

const replayTotal = computed(() => detailSteps.value.length)
const replayStep = computed(
  () => detailSteps.value.find((s) => s.step_no === replayIndex.value) || null,
)
const replayToggleLabel = computed(() => {
  if (!replayIndex.value) return '▶ 回放轨迹'
  if (replayIndex.value >= replayTotal.value) return '▶ 重新回放'
  return '▶ 继续回放'
})

function replayIntervalMs() {
  const found = REPLAY_SPEEDS.find((s) => s.key === replaySpeed.value)
  return (found || REPLAY_SPEEDS[1]).ms
}

function clearReplayTimer() {
  if (replayTimer) {
    clearInterval(replayTimer)
    replayTimer = null
  }
}

function scrollToActiveStep() {
  // 回放时自动把当前步骤滚进视野：评委的视线不用自己追。
  requestAnimationFrame(() => {
    const el = stepsRef.value?.querySelector('.step--active')
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  })
}

function replayTick() {
  if (replayIndex.value >= replayTotal.value) {
    finishReplay()
    return
  }
  replayIndex.value += 1
  scrollToActiveStep()
  if (replayIndex.value >= replayTotal.value) finishReplay()
}

function startReplayTimer() {
  clearReplayTimer()
  replayTimer = setInterval(replayTick, replayIntervalMs())
}

function finishReplay() {
  clearReplayTimer()
  replaying.value = false
}

function toggleReplay() {
  if (replaying.value) {
    // 暂停：保留已点亮的进度，便于逐条讲解
    clearReplayTimer()
    replaying.value = false
    return
  }
  if (!detailSteps.value.length) return
  // 已播完再点 = 从头重播
  if (replayIndex.value >= replayTotal.value) replayIndex.value = -1
  if (replayIndex.value < 0) replayIndex.value = 0
  replaying.value = true
  startReplayTimer()
  scrollToActiveStep()
}

function stopReplay() {
  clearReplayTimer()
  replaying.value = false
  replayIndex.value = -1
}

function setReplaySpeed(key) {
  replaySpeed.value = key
  // 播放中切换速度要立刻生效，而不是等当前这一步走完
  if (replaying.value) startReplayTimer()
}

// ---------- 自动回放：让「决策生长」在自己长出来 ----------
// 为什么不真做流式推送：规则模式下一次派单在单次同步调用里毫秒级跑完，
// 「逐步推流」的视觉效果和这里的自动回放几乎等价，但代价是动 WS 消息契约
// + 内核加步骤钩子 + 契约守卫（约 4-6 个文件）。边际收益太低，改用零后端
// 改动的做法：run 进入终态后自动开始回放，逐条点亮 + 已有的解说字幕。
//
// 刻意不自动回放的情况：run 停在 waiting_approval。那一步的主角是
// 「审批队列出现了待决项」，自动回放会把操作员的注意力从审批拉走。
const TERMINAL_RUN_STATUSES = new Set(['succeeded', 'failed', 'cancelled', 'expired'])
const autoReplay = ref(true)
const autoReplayRunId = ref('')   // 已武装、等待终态自动回放的 run

function armAutoReplay(runId) {
  if (!autoReplay.value || !runId) return
  autoReplayRunId.value = runId
}

// 从第一步开始回放（与「继续回放」区分：这里永远从头开始）
function startReplayFromBeginning() {
  if (!detailSteps.value.length) return
  clearReplayTimer()
  replayIndex.value = 0
  replaying.value = true
  startReplayTimer()
  scrollToActiveStep()
}

// 终态落地 → 自动起步。
// 等 nextTick 是必要的：回放要靠 querySelector('.step--active') 把当前步滚进
// 视野，而步骤行是这一帧才渲染出来的，同步去找会拿到空节点、自动滚动失效。
watch(
  [
    () => detailRun.value?.run_id,
    () => detailRun.value?.status,
    () => detailSteps.value.length,
  ],
  () => {
    const run = detailRun.value
    if (!autoReplay.value || !run || !detailSteps.value.length) return
    if (autoReplayRunId.value !== run.run_id) return
    if (!TERMINAL_RUN_STATUSES.has(run.status)) return
    // 先清标记再起步：否则回放过程中的任何一次详情刷新都会再触发一遍
    autoReplayRunId.value = ''
    nextTick(() => startReplayFromBeginning())
  },
)

// ======================= 取消运行 =======================
function canCancelRun(run) {
  return RUNNING_STATES.has(run.status)
}

function onCancel(run) {
  if (cancelArmed.value !== run.run_id) {
    cancelArmed.value = run.run_id
    return
  }
  doCancel(run)
}

async function doCancel(run) {
  cancelArmed.value = ''
  try {
    await agentsApi.cancelRun(run.run_id, { reason: '控制台操作员取消' })
    store.error = ''
    await Promise.all([
      loadRuns({ page: runsMeta.page, silent: true }),
      loadRuntime({ silent: true }),
    ])
    if (expandedRunId.value === run.run_id) await loadRunDetail(run.run_id)
  } catch (err) {
    store.error = agentErrText(err)
  }
}

// ======================= 启动运行（POST /runs，幂等） =======================
async function startRun() {
  // 按钮 :disabled 挡不住输入框 @keyup.enter 的连发，函数入口再拦一次
  if (starting.value) return
  const eventId = startForm.event_id.trim()
  if (!eventId) {
    store.error = '请输入事件编号（event_id）'
    return
  }
  starting.value = true
  try {
    // mode 只在多角色受支持时才随请求发出：后端不认识 'single' 之外的取值
    // （rule_plans 里没有的键会响亮失败），不传即走默认单角色计划。
    const mode = teamModeSupported.value ? startForm.mode : undefined
    const run = await agentsApi.createRun({
      event_id: eventId,
      idempotency_key: startForm.idempotency_key.trim() || undefined,
      mode,
    })
    startForm.idempotency_key = ''
    store.error = run?.idempotent_replay ? '幂等重放：返回既有运行（重复提交）' : ''
    // 武装自动回放：等这个 run 进终态（同步成功、或审批通过后）就自动起步。
    // 停在 waiting_approval 时不触发 —— 那时候该看的是审批队列。
    armAutoReplay(run?.run_id)
    await Promise.all([
      loadRuns({ page: 1, silent: true }),
      loadRuntime({ silent: true }),
      loadApprovals({ silent: true }),
    ])
    if (run?.run_id) {
      await loadRunDetail(run.run_id)
      syncQuery({ run: run.run_id, page: 1 })
    }
  } catch (err) {
    store.error = agentErrText(err)
  } finally {
    starting.value = false
  }
}

// ======================= 审批决定 =======================
async function decide(approval, decision) {
  if (deciding.value === approval.approval_id) return
  deciding.value = approval.approval_id
  try {
    await agentsApi.decideApproval(approval.approval_id, {
      decision,
      reason: decision === 'approved' ? '控制台批准（高风险动作放行）' : '控制台拒绝',
    })
    store.error = ''
    await Promise.all([
      loadApprovals({ silent: true }),
      loadRuns({ page: runsMeta.page, silent: true }),
      loadRuntime({ silent: true }),
    ])
    if (expandedRunId.value === approval.run_id) {
      // 审批人可能是另一台设备/另一个账号，他那边从没「启动」过这个 run，
      // 也就没被武装过自动回放；批准这一刻补上，让「批准 → 逐步执行」连贯呈现。
      armAutoReplay(approval.run_id)
      await loadRunDetail(approval.run_id)
    }
  } catch (err) {
    // 6003 已决策 → 刷新队列，把「已过期」的待决定项清掉
    if (err.code === 6003) await loadApprovals({ silent: true })
    store.error = agentErrText(err)
  } finally {
    deciding.value = ''
  }
}

// ======================= 轮询（轻量：runtime + 审批 + 打开的详情） =======================
let pollTimer = null
function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    loadRuntime({ silent: true })
    loadApprovals({ silent: true })
    // 回放进行中不刷详情：轮询回来的新 steps 数组会把已点亮的进度冲掉，
    // 演示录屏时表现为"回放到一半跳回第一步"。暂停回放时照常刷新。
    if (replaying.value) return
    if (expandedRunId.value && RUNNING_STATES.has(detailRun.value?.status)) {
      refreshDetail(expandedRunId.value)
    }
  }, 15000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ======================= query 同步 =======================
function syncQuery(patch) {
  router.replace({ query: { ...route.query, ...patch } }).catch(() => {})
}
function clearQuery(key) {
  const q = { ...route.query }
  delete q[key]
  router.replace({ query: q }).catch(() => {})
}

// query 直达：?run=<id> / ?page=<n>
watch(
  () => props.runId,
  (id) => {
    if (booting.value) return
    if (id) {
      if (expandedRunId.value !== id) openRunDetail(id)
    } else if (expandedRunId.value) {
      stopReplay()
      expandedRunId.value = ''
      detailRun.value = null
      detailSteps.value = []
      detailError.value = ''
    }
  },
)

watch(
  () => props.page,
  (p) => {
    if (booting.value) return
    if (p && p !== runsMeta.page) {
      runsMeta.page = p
      loadRuns({ page: p })
    }
  },
)

// ======================= 展示辅助 =======================
const pendingApprovals = computed(() => approvals.value.filter((a) => !a.decision))
const decidedApprovals = computed(() => approvals.value.filter((a) => a.decision))
const passedCount = computed(() => (evals.value?.scenarios || []).filter((s) => s.passed).length)

// 跨 run 经验库：直接取运行时快照里的 lessons（真源在内核 LessonStore），
// 拿不到就是空数组 —— 空态说明原因，不写死条数、不伪造占位。
const lessons = computed(() => runtime.value?.lessons || [])

// ---------- 评测：叙事抬头 ----------
const evalScenarioGroups = computed(() => {
  const buckets = { fault: [], disturb: [], baseline: [] }
  for (const sc of evals.value?.scenarios || []) {
    buckets[evalGroupKeyOf(sc)].push(sc)
  }
  return EVAL_SCENARIO_GROUPS.map((g) => {
    const items = buckets[g.key] || []
    return { ...g, items, passed: items.filter((s) => s.passed).length }
  })
})

const evalNarrative = computed(() => {
  if (!evals.value) return ''
  const total = (evals.value.scenarios || []).length
  const fault = evalScenarioGroups.value.find((g) => g.key === 'fault')?.items.length || 0
  const disturb = evalScenarioGroups.value.find((g) => g.key === 'disturb')?.items.length || 0
  if (!total) return ''
  return (
    `结论不是「跑通了一次」：${total} 个固定场景 = ${fault} 个故意要求它安全停下、`
    + `${total - fault} 个要求它走完闭环（其中 ${disturb} 个还额外注入了模型异常 / 重启 / `
    + `并发 / 设备故障 / 多角色 / 跨 run 状态等干扰），最终 ${passedCount.value}/${total} 通过；`
    + `每一步都留下输入/输出哈希，可离线逐条回放复核。`
  )
})

// ---------- 评测：质量雷达 ----------
function radarValue(metric) {
  const raw = evals.value?.metrics?.[metric.key]
  if (raw == null) return null
  const num = Number(raw)
  if (!Number.isFinite(num)) return null
  return metric.inverted ? 1 - num : num
}

const evalRadarDims = computed(() =>
  EVAL_RADAR_GROUPS.map((name) => {
    const items = EVAL_RADAR_METRICS.filter((m) => m.group === name)
    const values = items.map(radarValue).filter((v) => v != null)
    return {
      name,
      score: values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0,
    }
  }),
)

const evalRadarOption = computed(() => {
  const indicators = EVAL_RADAR_METRICS.map((m) => ({
    name: `${m.group}·${m.label}`,
    max: 1,
    min: 0,
  }))
  // 缺值的指标画成 0 会误导（看上去像"这项得了 0 分"），
  // 所以用 null 让 ECharts 留空并在 tooltip 里显示"—"。
  const values = EVAL_RADAR_METRICS.map((m) => {
    const v = radarValue(m)
    return v == null ? null : Number(v.toFixed(4))
  })
  return {
    tooltip: { trigger: 'item' },
    radar: {
      indicator: indicators,
      radius: '62%',
      center: ['50%', '52%'],
      axisName: { color: '#8b96a8', fontSize: 10.5 },
      splitLine: { lineStyle: { color: 'rgba(142, 142, 147, 0.24)' } },
      splitArea: { areaStyle: { color: ['rgba(142, 142, 147, 0.04)', 'rgba(142, 142, 147, 0.02)'] } },
      axisLine: { lineStyle: { color: 'rgba(142, 142, 147, 0.28)' } },
    },
    series: [
      {
        type: 'radar',
        symbolSize: 4,
        data: [
          {
            value: values,
            name: '质量指标',
            lineStyle: { width: 2, color: '#007aff' },
            itemStyle: { color: '#007aff' },
            areaStyle: { color: 'rgba(0, 122, 255, 0.18)' },
          },
        ],
      },
    ],
  }
})

function runStatusMeta(s) {
  return RUN_STATUS_META[s] || { label: s || '未知', color: '#8e8e93' }
}
function stepTypeMeta(t) {
  return STEP_TYPE_META[t] || { label: t || '步骤', glyph: '·', color: '#8e8e93' }
}
function stepStatusMeta(s) {
  return STEP_STATUS_META[s] || { label: s || '—', color: '#8e8e93' }
}
function stepErrLabel(code) {
  return STEP_ERROR_LABELS[code] || code || '—'
}
function riskMeta(r) {
  return RISK_META[r] || { label: r || '—', color: '#8e8e93' }
}
function agentStatusMeta(s) {
  return (
    {
      running: { label: '运行中', cls: '--running' },
      idle: { label: '空闲', cls: '--idle' },
      unavailable: { label: '不可用', cls: '--unavailable' },
    }[s] || { label: s || '未知', cls: '--unknown' }
  )
}
function triggerLabel(t) {
  return TRIGGER_LABELS[t] || t || '—'
}
// 角色色带：研判＝紫色（分析）、调度执行＝蓝色（动作）。
// 未知角色走中性灰，不假装认识它。
const ROLE_TONES = {
  '事件研判 Agent': 'assess',
  '调度执行 Agent': 'dispatch',
}
function roleClass(role) {
  const tone = ROLE_TONES[role]
  return tone ? `role--${tone}` : 'role--other'
}
function fmtUptime(ms) {
  if (ms == null) return '—'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}
function fmtMs(ms) {
  if (ms == null) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}
function fmtMetric(key, v) {
  if (v == null || v === '') return '—'
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v)
  if (key === 'p95_decision_latency_ms') return fmtMs(v)
  return `${(v * 100).toFixed(1)}%`
}
function boolLabel(v) {
  if (v === true) return '可用'
  if (v === false) return '不可用'
  return '—'
}
function jsonPretty(obj) {
  try {
    return JSON.stringify(obj || {}, null, 2)
  } catch {
    return String(obj || '')
  }
}
function toggleSchema(name) {
  expandedSchemas[name] = !expandedSchemas[name]
}

// ======================= 生命周期 =======================
onMounted(() => {
  bootstrap()
  startPolling()
})

onBeforeUnmount(() => {
  stopReplay()
  stopPolling()
})
</script>

<style scoped>
.agents {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}

/* ---------- 头部 ---------- */
.agents__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.agents__title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-main);
  letter-spacing: 1px;
}

.agents__sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-sub);
}

.agents__head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* ---------- Runtime 徽标 ---------- */
.runtime-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 12px;
  border-radius: 16px;
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.4px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  white-space: nowrap;
}

.runtime-badge__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

.runtime-badge--idle { color: var(--c-success); border-color: rgba(61, 220, 132, 0.4); }
.runtime-badge--running {
  color: var(--c-primary);
  border-color: rgba(24, 224, 200, 0.45);
}
.runtime-badge--running .runtime-badge__dot {
  animation: pulse-ring 1.8s ease-out infinite;
}
.runtime-badge--unavailable { color: var(--c-danger); border-color: rgba(242, 86, 76, 0.45); }
.runtime-badge--loading { color: var(--text-sub); }
.runtime-badge--error { color: var(--c-danger); border-color: rgba(242, 86, 76, 0.45); }
.runtime-badge--unknown { color: var(--text-sub); }

/* ---------- 全局状态横幅 ---------- */
.agents__banner {
  padding: 12px 16px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}

.agents__banner--failed { background: rgba(242, 86, 76, 0.1); border-color: rgba(242, 86, 76, 0.45); }
.agents__banner--unavailable { background: rgba(245, 166, 35, 0.1); border-color: rgba(245, 166, 35, 0.45); }
.agents__banner--degraded { background: rgba(245, 166, 35, 0.08); border-color: rgba(245, 166, 35, 0.4); }

.agents__banner-title { font-weight: 600; color: var(--text-main); }
.agents__banner-text { color: var(--text-sub); }
.agents__banner-text code { color: var(--c-warn); }
.agents__banner-list { margin: 2px 0 4px 18px; color: var(--text-sub); display: flex; flex-direction: column; gap: 2px; }
.agents__retry {
  margin-left: 8px;
  padding: 0 8px;
  font-size: 12px;
  color: var(--c-primary);
  background: none;
  border: 1px solid var(--c-primary-dim);
  border-radius: 4px;
  cursor: pointer;
}
.agents__retry:hover { background: rgba(24, 224, 200, 0.12); }

/* ---------- 运行时总览 ---------- */
.agents__overview {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.agents__ov-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  min-width: 0;
}

.agents__ov-k { font-size: 12px; color: var(--text-sub); }
.agents__ov-v { font-size: 14px; font-weight: 600; color: var(--text-main); }
.agents__ov-v--warn { color: var(--c-warn); }
.agents__ov-v--mono { font-family: 'SF Mono', Consolas, monospace; font-size: 12.5px; }

/* ---------- 主区域网格 ---------- */
.agents__sections {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
}

.agents__runs { grid-column: 1 / -1; }

/* ---------- 面板内通用工具 ---------- */
.agents__select {
  padding: 4px 8px;
  font-size: 12px;
  color: var(--text-main);
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  outline: none;
}

.agents__runs-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.mono { font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace; }
.dim { color: var(--text-sub); }

.table-scroll { overflow-x: auto; }

/* ---------- 启动运行表单 ---------- */
.agents__start {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel-2);
}

.agents__start-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.agents__start-row .form-input { flex: 1; min-width: 180px; }

/* ---------- Runs 表格 ---------- */
.runs-table__row { cursor: pointer; }
.runs-table__row--open { background: var(--bg-hover); }
.agents__th-actions { width: 64px; text-align: right; }
.agents__expand { font-size: 12px; color: var(--c-primary); white-space: nowrap; }

.agents__status {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 11px;
  font-size: 11.5px;
  line-height: 18px;
  border: 1px solid transparent;
  white-space: nowrap;
}

.agents__chip {
  display: inline-block;
  margin-left: 6px;
  padding: 0 7px;
  border-radius: 9px;
  font-size: 10.5px;
  line-height: 17px;
  color: var(--text-sub);
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  white-space: nowrap;
  vertical-align: 1px;
}
.agents__chip--replay { color: var(--c-info); border-color: rgba(74, 158, 255, 0.4); }
.agents__chip--warn { color: var(--c-warn); border-color: rgba(245, 166, 35, 0.4); }
.agents__chip--ok { color: var(--c-success); border-color: rgba(61, 220, 132, 0.4); }
.agents__chip--no { color: var(--c-danger); border-color: rgba(242, 86, 76, 0.4); }

.agents__count-badge {
  padding: 1px 9px;
  border-radius: 10px;
  font-size: 11px;
  color: var(--c-primary);
  background: rgba(24, 224, 200, 0.1);
  border: 1px solid rgba(24, 224, 200, 0.3);
}

/* ---------- 分页 ---------- */
.agents__pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 10px 16px;
}

.agents__pager-text { font-size: 12px; color: var(--text-sub); }

/* ---------- Run 详情 ---------- */
.run-detail {
  border-top: 1px solid var(--border);
  background: var(--bg-panel-2);
}

.run-detail__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.run-detail__title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  font-weight: 600;
}

.run-detail__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

.run-detail__meta {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
}

.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 0;
  font-size: 12.5px;
  border-bottom: 1px solid rgba(28, 42, 58, 0.5);
}
.kv:last-child { border-bottom: none; }
.kv__k { color: var(--text-sub); flex-shrink: 0; }
.kv__v { color: var(--text-main); text-align: right; word-break: break-all; min-width: 0; }

/* ---------- Steps 时间线 ---------- */
.steps { padding: 10px 16px 14px; }

.step {
  display: grid;
  grid-template-columns: 30px 104px minmax(0, 1fr) auto;
  column-gap: 10px;
  align-items: baseline;
  padding: 8px 2px 8px 0;
  margin-left: 13px;
  padding-left: 16px;
  border-left: 2px solid rgba(46, 74, 102, 0.55);
  position: relative;
  transition: opacity 0.2s;
}

.step::before {
  content: '';
  position: absolute;
  left: -7px;
  top: 15px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--bg-panel-2);
  border: 2px solid var(--step-color, var(--text-sub));
}

.step--hidden { opacity: 0.18; }

/* 多角色步骤多一列角色徽标；单角色行仍然是原来的四列，布局不变 */
.step--roled { grid-template-columns: 30px 104px auto minmax(0, 1fr) auto; }

/* 回放中当前点亮的那一步：整行提亮 + 左侧竖线加粗，视线自然落到它身上。
   用 .step.step--active 提高权重：文件后段的 iOS 覆写里有 `.step { border-left-color }`，
   同权重时后写的会赢，直接用 .step--active 会被它悄悄盖掉。 */
.step.step--active {
  background: color-mix(in srgb, var(--c-primary) 9%, transparent);
  border-left-color: var(--c-primary);
  border-left-width: 3px;
  margin-left: 12px;
  border-radius: 0 8px 8px 0;
}

.step--active::before {
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--c-primary) 22%, transparent);
}

.step__role {
  align-self: center;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 10.5px;
  white-space: nowrap;
  border: 1px solid transparent;
}

.role--assess {
  color: #7a5af8;
  border-color: rgba(122, 90, 248, 0.35);
  background: rgba(122, 90, 248, 0.1);
}

.role--dispatch {
  color: #007aff;
  border-color: rgba(0, 122, 255, 0.3);
  background: rgba(0, 122, 255, 0.09);
}

.role--other {
  color: var(--text-sub);
  border-color: var(--border);
  background: var(--bg-panel-2);
}

/* ---------- 回放控制条与当前步骤解说 ---------- */
.replay-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 16px 2px;
}

.replay-speeds {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border-radius: 999px;
  background: var(--bg-panel-2);
}

.replay-speed {
  padding: 3px 10px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
}

.replay-speed:hover { color: var(--text-main); }

.replay-speed--on {
  background: var(--c-primary);
  color: #ffffff;
}

.replay-progress { margin-left: auto; font-size: 11.5px; color: var(--text-sub); }

.replay-caption {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin: 8px 16px 0;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--c-primary) 30%, transparent);
  background: color-mix(in srgb, var(--c-primary) 7%, transparent);
}

.replay-caption__role {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  white-space: nowrap;
  border: 1px solid transparent;
}

.replay-caption__type { font-size: 11.5px; white-space: nowrap; }
.replay-caption__text { font-size: 12.5px; color: var(--text-main); min-width: 0; }

/* ---------- 自动回放开关（启动表单里） ---------- */
.agents__auto-replay {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 4px;
  font-size: 12px;
  color: var(--text-sub);
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}

.agents__auto-replay input {
  width: 15px;
  height: 15px;
  accent-color: var(--c-primary);
  cursor: pointer;
}

.agents__auto-replay:hover { color: var(--text-main); }

/* ---------- 跨 run 经验库 ---------- */.lessons__empty { line-height: 1.75; }

.lessons {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
  padding: 4px 14px 14px;
}

.lesson {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--bg-panel-2);
}

.lesson__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11.5px;
}

.lesson__scope {
  padding: 1px 7px;
  border-radius: 999px;
  background: color-mix(in srgb, #7a5af8 12%, transparent);
  color: #7a5af8;
}

.lesson__kind { color: var(--text-dim); font-size: 11px; }
.lesson__conf { margin-left: auto; color: var(--c-success); }
.lesson__hits { color: var(--text-sub); }

.lesson__cond {
  margin-top: 7px;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.6;
}

.lesson__guide {
  margin-top: 3px;
  font-size: 12.5px;
  color: var(--text-main);
  line-height: 1.6;
}

/* ---------- 派单决策快照（Run 详情） ---------- */
.run-decision {
  margin: 10px 16px 0;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg-panel-2);
}

.run-decision__head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
}

.run-decision__title { font-weight: 600; color: var(--text-main); }
.run-decision__picked { color: var(--text-main); }
.run-decision__reason { color: var(--text-sub); }

.run-decision__table { margin-top: 8px; width: 100%; font-size: 11.5px; }

.run-decision__row--picked {
  background: color-mix(in srgb, var(--c-primary) 8%, transparent);
}

.run-decision__basis { margin-top: 8px; font-size: 11.5px; color: var(--text-sub); }
.run-decision__basis-title { color: var(--text-dim); margin-bottom: 3px; }
.run-decision__basis ul { margin: 0; padding-left: 18px; }
.run-decision__basis li { line-height: 1.65; }

/* ---------- 协同角色链（Run 详情头部） ---------- */.run-roles {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 16px 0;
}

.run-roles__label { font-size: 12px; color: var(--text-dim); }

.run-roles__item {
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  border: 1px solid transparent;
}

.run-roles__arrow { color: var(--text-dim); font-size: 12px; }

.agents__start-hint {
  padding: 6px 2px 0;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.5;
}

.step__no { font-size: 11px; color: var(--text-dim); text-align: right; }
.step__type { font-size: 12px; color: var(--step-color, var(--text-sub)); white-space: nowrap; }
.step__summary { font-size: 12.5px; color: var(--text-main); min-width: 0; }
.step__tool { margin-left: 8px; font-size: 11px; color: var(--c-primary); }
.step__hash { margin-left: 8px; font-size: 10.5px; color: var(--text-dim); cursor: help; }
.step__meta { display: flex; align-items: center; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
.step__status { font-size: 11.5px; white-space: nowrap; }
.step__err {
  font-size: 11px;
  color: var(--c-danger);
  border: 1px solid rgba(242, 86, 76, 0.4);
  border-radius: 8px;
  padding: 0 6px;
  line-height: 16px;
  white-space: nowrap;
  cursor: help;
}
.step__lat { font-size: 11px; color: var(--text-dim); white-space: nowrap; }

/* ---------- 审批 ---------- */
.approvals { padding: 4px 14px 14px; display: flex; flex-direction: column; gap: 10px; }

.approval {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: var(--bg-panel-2);
}

.approval--pending { border-color: rgba(245, 166, 35, 0.45); }

.approval__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.approval__risk {
  padding: 0 8px;
  border-radius: 9px;
  font-size: 10.5px;
  line-height: 17px;
  border: 1px solid transparent;
  white-space: nowrap;
}

.approval__body { margin-top: 8px; }
.approval__action { font-size: 13px; color: var(--text-main); }
.approval__meta {
  margin-top: 5px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 11.5px;
  color: var(--text-sub);
}

.approval__actions {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.btn--approve { color: var(--c-success); border-color: rgba(61, 220, 132, 0.5); background: rgba(61, 220, 132, 0.08); }
.btn--approve:hover:not(:disabled) { color: var(--c-success); background: rgba(61, 220, 132, 0.18); border-color: var(--c-success); }
.btn--reject { color: var(--c-danger); border-color: rgba(242, 86, 76, 0.5); background: rgba(242, 86, 76, 0.08); }
.btn--reject:hover:not(:disabled) { color: var(--c-danger); background: rgba(242, 86, 76, 0.18); border-color: var(--c-danger); }

.approval__hint { font-size: 11px; color: var(--text-dim); }

/* ---------- 工具目录 ---------- */
.tools__name { font-size: 12.5px; color: var(--c-primary); }
.tools__desc { margin-top: 2px; font-size: 11.5px; color: var(--text-sub); }
.tools__schema-toggle {
  margin-top: 5px;
  padding: 0;
  font-size: 11px;
  color: var(--c-info);
  background: none;
  border: none;
  cursor: pointer;
}
.tools__schema-toggle:hover { text-decoration: underline; }

.tools__schema {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.tools__schema-title { font-size: 11.5px; color: var(--text-sub); margin-bottom: 5px; }

.tools__schema pre,
.eval__denoms pre {
  margin: 0;
  padding: 8px 10px;
  background: rgba(3, 6, 10, 0.55);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-sub);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ---------- 评测摘要 ---------- */
.eval { padding: 12px 16px 14px; }

.eval__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--text-sub);
}

.eval__tag {
  padding: 1px 8px;
  border-radius: 9px;
  border: 1px solid var(--border);
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
}

.eval__date { margin-left: auto; }

.eval__score { margin: 14px 0 12px; text-align: center; }
.eval__score-num {
  font-size: 34px;
  font-weight: 700;
  background: var(--grad-primary);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  font-variant-numeric: tabular-nums;
}
.eval__score-den { font-size: 18px; opacity: 0.7; }
.eval__score-label { margin-top: 2px; font-size: 12px; color: var(--text-sub); }

.eval__metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.eval__metric {
  padding: 8px 10px;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  min-width: 0;
}

.eval__metric-k { display: block; font-size: 11px; color: var(--text-sub); }
.eval__metric-v { display: block; margin-top: 2px; font-size: 15px; font-weight: 600; color: var(--text-main); font-variant-numeric: tabular-nums; }

.eval__detail { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.eval__denoms pre { white-space: pre-wrap; }

.eval__scenario {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  margin-top: 8px;
  background: var(--bg-panel-2);
}

.eval__scenario-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.eval__scenario-name { color: var(--text-main); }
.eval__scenario-body {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11.5px;
  color: var(--text-sub);
}

.eval__scenario-desc {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.6;
}

/* ---------- 评测叙事：抬头 + 质量雷达 + 场景分组 ---------- */
.eval__narrative {
  margin: 0 16px 4px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--c-primary) 24%, transparent);
  background: color-mix(in srgb, var(--c-primary) 6%, transparent);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-main);
}

.eval__radar {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(240px, 1fr);
  gap: 12px;
  align-items: center;
  padding: 4px 12px 0;
}

.eval__radar-side {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.eval__radar-note {
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.65;
}

.eval__radar-dims {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.eval__radar-dim {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr) 42px;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-main);
}

.eval__radar-dim-bar {
  height: 7px;
  border-radius: 999px;
  background: var(--bg-panel-2);
  overflow: hidden;
}

.eval__radar-dim-bar i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--grad-primary, #007aff);
}

.eval__radar-dim-score { text-align: right; font-size: 11.5px; color: var(--text-sub); }

.eval__radar-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.eval__group { margin-top: 14px; }

.eval__group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.eval__group-title { font-size: 13px; font-weight: 600; color: var(--text-main); }

.eval__group-hint { font-size: 11.5px; color: var(--text-dim); }

@media (max-width: 900px) {
  .eval__radar {
    grid-template-columns: minmax(0, 1fr);
  }
}

.agents__eval-empty {
  padding: 30px 20px;
  text-align: center;
}
.agents__eval-empty-title { font-size: 15px; font-weight: 600; color: var(--text-main); }
.agents__eval-empty-text { margin-top: 8px; font-size: 12.5px; color: var(--text-sub); line-height: 1.7; }
.agents__eval-empty-text code {
  font-family: 'SF Mono', Consolas, monospace;
  color: var(--c-warn);
  background: rgba(245, 166, 35, 0.1);
  padding: 0 5px;
  border-radius: 4px;
}

/* ---------- 能力定义卡片 ---------- */
.agents__grid-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  padding: 14px 16px;
}

.agent-card {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 16px 14px;
  min-width: 0;
  animation: rise-in 0.5s var(--ease) both;
  transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}

.agent-card:hover {
  transform: translateY(-3px);
  border-color: var(--border-bright);
  box-shadow: var(--shadow-glow);
}

.agent-card__top { display: flex; align-items: center; gap: 12px; }
.agent-card__badge {
  width: 42px;
  height: 42px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 19px;
  font-weight: 600;
  color: #06251f;
  background: var(--grad-primary);
  border-radius: 12px;
}
.agent-card__id { flex: 1; min-width: 0; }
.agent-card__name { font-size: 15px; font-weight: 600; color: var(--text-main); }
.agent-card__codename { font-size: 12px; color: var(--c-primary); letter-spacing: 1px; }

.agent-card__status {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  white-space: nowrap;
}
.agent-card__status .agent-card__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.agent-card__status--idle { color: var(--c-success); }
.agent-card__status--running { color: var(--c-primary); }
.agent-card__status--running .agent-card__dot { animation: pulse-ring 1.8s ease-out infinite; }
.agent-card__status--unavailable { color: var(--c-danger); }
.agent-card__status--unknown { color: var(--text-sub); }

.agent-card__role { margin-top: 10px; font-size: 12px; color: var(--text-sub); }
.agent-card__desc { margin-top: 8px; font-size: 12.5px; line-height: 1.6; color: var(--text-sub); }
.agent-card__caps { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.agent-card__cap {
  padding: 2px 9px;
  font-size: 11px;
  color: var(--text-sub);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
}

/* ---------- 骨架屏 ---------- */
.agents__skel-list {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skel {
  background: linear-gradient(90deg, rgba(255, 255, 255, 0.04) 25%, rgba(255, 255, 255, 0.1) 37%, rgba(255, 255, 255, 0.04) 63%);
  background-size: 400% 100%;
  animation: skel-shimmer 1.4s ease infinite;
  border-radius: 6px;
}

@keyframes skel-shimmer {
  0% { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}

/* ---------- 模块级错误态 ---------- */
.agents__module-error {
  margin: 10px 16px;
  padding: 12px 14px;
  border: 1px solid rgba(242, 86, 76, 0.4);
  border-radius: var(--radius);
  background: rgba(242, 86, 76, 0.08);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--c-danger);
}

/* ---------- 响应式 ---------- */
@media (max-width: 1400px) {
  .agents__grid-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .eval__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 1100px) {
  .eval__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 768px) {
  .agents__sections { grid-template-columns: 1fr; }
  .agents__grid-cards { grid-template-columns: 1fr; }
  .hide-sm { display: none; }
  .step { grid-template-columns: 24px 96px minmax(0, 1fr); }
  .step__meta { grid-column: 2 / -1; justify-content: flex-start; }
  .tools__schema { grid-template-columns: 1fr; }
  .eval__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .agents__ov-chip { flex: 1 1 42%; }
}

/* ---------- iOS 风格收敛 ---------- */
.agents {
  letter-spacing: 0;
}

.agents__title {
  letter-spacing: 0;
}

.runtime-badge,
.agents__status,
.agents__chip,
.agents__count-badge,
.approval__risk,
.eval__tag {
  border-radius: 999px;
}

.runtime-badge {
  background: var(--bg-panel);
  font-weight: 500;
}

.runtime-badge--idle { border-color: rgba(52, 199, 89, 0.35); }
.runtime-badge--running { border-color: rgba(0, 122, 255, 0.35); }
.runtime-badge--unavailable,
.runtime-badge--error { border-color: rgba(255, 59, 48, 0.35); }

.agents__banner {
  border-radius: 12px;
}

.agents__banner--failed { background: rgba(255, 59, 48, 0.08); border-color: rgba(255, 59, 48, 0.3); }
.agents__banner--unavailable,
.agents__banner--degraded { background: rgba(255, 149, 0, 0.08); border-color: rgba(255, 149, 0, 0.28); }

.agents__retry {
  min-height: 32px;
  border: 0;
  border-radius: 8px;
  background: var(--bg-active);
}

.agents__ov-chip {
  min-height: 48px;
  border-color: var(--panel-border);
}

.agents__sections {
  gap: 12px;
}

.agents__select {
  min-height: 34px;
  border-radius: 9px;
  background: var(--bg-panel-2);
}

.table-scroll {
  max-width: 100%;
  overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
}

.agents__start {
  background: var(--bg-panel-2);
  border-bottom-color: var(--separator);
}

.agents__chip {
  background: var(--bg-panel-2);
}

.agents__chip--replay { color: var(--c-info); border-color: rgba(0, 122, 255, 0.3); }
.agents__chip--warn { color: var(--c-warn); border-color: rgba(255, 149, 0, 0.3); }
.agents__chip--ok { color: var(--c-success); border-color: rgba(52, 199, 89, 0.3); }
.agents__chip--no { color: var(--c-danger); border-color: rgba(255, 59, 48, 0.3); }

.agents__count-badge {
  border: 0;
  background: var(--bg-active);
}

.run-detail {
  background: var(--bg-panel-2);
  border-top-color: var(--separator);
}

.run-detail__head,
.run-detail__meta {
  border-bottom-color: var(--separator);
}

.kv {
  border-bottom-color: var(--separator);
}

.step {
  border-left-color: rgba(142, 142, 147, 0.32);
}

.step::before {
  background: var(--bg-panel-2);
}

.step__err {
  border-color: rgba(255, 59, 48, 0.3);
}

.approval {
  border-color: var(--panel-border);
  border-radius: 12px;
  background: var(--bg-panel-2);
}

.approval--pending {
  border-color: rgba(255, 149, 0, 0.35);
}

.btn--approve {
  color: var(--c-success);
  border-color: rgba(52, 199, 89, 0.35);
  background: rgba(52, 199, 89, 0.08);
}

.btn--approve:hover:not(:disabled) {
  color: var(--c-success);
  border-color: rgba(52, 199, 89, 0.5);
  background: rgba(52, 199, 89, 0.14);
}

.btn--reject {
  color: var(--c-danger);
  border-color: rgba(255, 59, 48, 0.35);
  background: rgba(255, 59, 48, 0.08);
}

.btn--reject:hover:not(:disabled) {
  color: var(--c-danger);
  border-color: rgba(255, 59, 48, 0.5);
  background: rgba(255, 59, 48, 0.14);
}

.tools__schema pre,
.eval__denoms pre {
  background: var(--bg-page);
  border-color: var(--panel-border);
  border-radius: 10px;
}

.eval__score-num {
  color: var(--text-main);
  background: none;
  -webkit-text-fill-color: currentColor;
}

.eval__metric {
  border-color: var(--panel-border);
  border-radius: 11px;
}

.agent-card {
  border-color: var(--panel-border);
  border-radius: 14px;
}

.agent-card:hover {
  transform: none;
  border-color: var(--panel-border);
  box-shadow: none;
}

.agent-card__badge {
  color: #ffffff;
  background: var(--c-primary);
  border-radius: 11px;
}

.skel {
  background: linear-gradient(
    90deg,
    var(--bg-panel-2) 25%,
    var(--bg-hover) 37%,
    var(--bg-panel-2) 63%
  );
  background-size: 400% 100%;
}

.agents__module-error {
  border-color: rgba(255, 59, 48, 0.3);
  background: rgba(255, 59, 48, 0.07);
  border-radius: 12px;
}

@media (max-width: 768px) {
  .agents {
    height: auto;
    min-height: 100%;
    gap: 12px;
  }

  .agents__head {
    align-items: flex-start;
  }

  .agents__head-right {
    width: 100%;
  }

  .agents__head-right .btn {
    flex: 1;
  }

  .agents__overview {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .agents__ov-chip {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
    padding: 9px 11px;
  }

  .agents__ov-v {
    font-size: 15px;
  }

  .agents__runs-tools {
    width: 100%;
    justify-content: flex-end;
  }

  .agents__start {
    padding: 10px 12px;
  }

  .agents__start-row .form-input {
    min-width: 0;
    flex-basis: 100%;
  }

  .agents__start-row .btn {
    width: 100%;
  }

  .runs-table {
    min-width: 720px;
  }

  .tools-table {
    min-width: 700px;
  }

  .agents__pager {
    justify-content: space-between;
    padding: 10px 12px;
  }

  .run-detail__head,
  .run-detail__meta,
  .steps {
    padding-inline: 12px;
  }

  .run-detail__actions {
    width: 100%;
  }

  .run-detail__actions .btn {
    flex: 1;
  }

  .step {
    grid-template-columns: 24px 90px minmax(0, 1fr);
    column-gap: 8px;
  }

  .step__meta {
    grid-column: 2 / -1;
    justify-content: flex-start;
  }

  .approvals {
    padding-inline: 12px;
  }

  .approval__actions .btn {
    min-width: 96px;
  }

  .agents__grid-cards,
  .eval {
    padding-inline: 12px;
  }
}

@media (max-width: 480px) {
  .agents__title {
    font-size: 17px;
  }

  .agents__sub {
    line-height: 1.6;
  }

  .agents__banner {
    padding: 12px;
  }

  .agents__banner-list {
    margin-left: 16px;
  }

  .eval__metrics {
    grid-template-columns: 1fr;
  }

  .eval__date {
    margin-left: 0;
  }

  .step {
    grid-template-columns: 24px minmax(0, 1fr);
  }

  .step__type,
  .step__summary,
  .step__meta {
    grid-column: 2;
  }

  .step__meta {
    margin-top: 4px;
  }

  .approval__actions .btn {
    flex: 1;
    min-width: 0;
  }
}

/* ============================================================================
   窄屏专项（手机 375–430px）：值班审批员现场镜头的可点可读适配
   ----------------------------------------------------------------------------
   演示里有一段镜头是"人在手机上批准高风险动作"，所以这里的取舍不是"好看"，
   而是"手指点得中、眼睛看得清、录屏不跳"。三类问题只在窄屏出现：
     1) 审批卡把 ID / 徽标 / 元信息塞在一行靠 flex-wrap 兜底，375px 上折成碎行，
        风险级别被压到看不清颜色；
     2) 回放条里 margin-left:auto 的进度文本一旦换行，会被顶到上一行按钮旁边；
     3) 轨迹行是固定列宽 grid（30px 104px …），固定列之和已超过屏宽，右侧状态被裁掉。
   下面只做"重排布 + 放大触控目标"，不碰任何数据、也不改桌面端（>900px）观感。
   全部用项目 CSS 变量 / 语义色，浅色与深色主题同样成立。
   ============================================================================ */

@media (max-width: 640px) {
  /* ---------- 回放控制条 ---------- */
  /* 手机上「播放 + 重置 + 三档速度 + 进度」一行排不下。把速度档位与进度各占一整行：
     进度必须清掉 margin-left:auto —— 换行后 auto 会把它顶到最右、紧贴上一行的按钮，
     手指去按播放时容易误触。 */
  .replay-bar {
    padding: 10px 12px 2px;
    gap: 8px;
  }

  .replay-bar > .btn {
    flex: 1 1 0;
    /* 主操作（回放/暂停）：录屏时手指落点会盖住按钮下缘，44px 是 iOS 推荐的最小触控高度 */
    min-height: 44px;
    font-size: 14px;
  }

  /* 三档速度独占一行并等分：既不和播放键抢宽度，每一档也更宽更好点 */
  .replay-speeds {
    flex: 1 1 100%;
    justify-content: space-between;
    padding: 3px;
  }

  .replay-speed {
    flex: 1 1 0;
    /* 次要操作 40px：速度切换用得少，不给它和主操作一样的视觉权重 */
    min-height: 40px;
    padding: 0 6px;
    font-size: 12.5px;
  }

  .replay-progress {
    margin-left: 0;
    flex: 1 1 100%;
    font-size: 12px;
  }

  /* ---------- 当前步骤解说 ---------- */
  /* 解说是"这一步在做什么"的整句话，和两个 nowrap 徽标挤一行会被压到只剩几个字 */
  .replay-caption {
    margin: 8px 12px 0;
    padding: 10px 12px;
    gap: 7px;
  }

  .replay-caption__text {
    /* 独占下一行，保证摘要能整句读完 */
    flex: 1 1 100%;
    /* 手机上多行文本行距太紧会连成一片，回放时读不清 */
    line-height: 1.6;
  }

  /* ---------- Steps 时间线 ---------- */
  /* 桌面端是 4/5 列固定宽度网格（30px 104px …），固定列之和在 375px 上超过可用宽度，
     表现为右侧"状态/耗时"被裁掉、或整页横向溢出。这里改成三行式：
       ① 步骤号 + 类型 + 角色   ② 摘要独占满宽   ③ 状态 / 错误码 / 耗时
     用 grid-template-areas 显式占位而不是靠 auto-flow：单角色步骤会少一个 .step__role
     子元素，auto-flow 会让其后的元素整体前移一格，行与行就对不齐了。 */
  .step,
  .step.step--roled {
    grid-template-columns: auto minmax(0, 1fr) auto;
    grid-template-areas:
      "no type role"
      "summary summary summary"
      "meta meta meta";
    align-items: center;
    column-gap: 8px;
    row-gap: 5px;
    /* 扣一点缩进把宽度还给内容；与下方 --active 的 margin-left 取同值，
       否则回放点亮某一行的瞬间会横向跳 2px */
    margin-left: 10px;
    padding-left: 12px;
    padding-right: 2px;
  }

  /* 单独列 .step--active：它在文件前段用 0,2,0 写了 margin-left，
     只写 0,1,0 的 .step 会被它压过，点亮步骤时会突然右移 */
  .step.step--active {
    margin-left: 10px;
  }

  .step__no {
    grid-area: no;
    text-align: left;
  }

  .step__type {
    grid-area: type;
  }

  .step__role {
    grid-area: role;
    justify-self: end;
    align-self: center;
  }

  .step__summary {
    grid-area: summary;
    line-height: 1.55;
    /* 工具名 / 哈希说明是连续无空格的英文串，不打断就会把这一格撑出屏幕 */
    overflow-wrap: anywhere;
  }

  .step__meta {
    grid-area: meta;
    /* 手机上靠左，与上面的步骤号对齐；桌面端的右对齐在窄屏会贴着被裁的边界 */
    justify-content: flex-start;
  }

  /* ---------- Run 详情头部 ---------- */
  .run-detail__head {
    gap: 8px;
    padding: 12px;
  }

  /* run_id 是长串等宽文本，单行放不下会把状态徽标顶出屏幕 */
  .run-detail__title {
    min-width: 0;
  }

  .run-detail__title .mono {
    overflow-wrap: anywhere;
  }

  .run-detail__actions .btn {
    /* 与审批按钮一致：回放 / 关闭在手机上一眼可见可点 */
    min-height: 44px;
  }

  /* ---------- 派单决策快照 ---------- */
  /* 表头四项（标题 / 选中 / 理由 / 建议）竖排，否则长理由会把"选中 xxx（123 m）"
     挤到第二行，看起来像两段互不相关的内容 */
  .run-decision {
    margin: 10px 12px 0;
    padding: 10px;
    /* 允许在 grid/flex 父级里收缩，避免把面板撑宽 */
    min-width: 0;
  }

  .run-decision__head {
    flex-direction: column;
    align-items: flex-start;
    gap: 5px;
  }

  /* 竖排后徽标自带的 margin-left 会形成一段莫名的缩进 */
  .run-decision__head .agents__chip {
    margin-left: 0;
  }

  /* 候选表：窄屏保持"可横向滚动"（外层 .table-scroll 负责滚动），
     给一个最小宽度让六列不至于被压成一字一行 —— 这是允许滚动，而不是压缩内容 */
  .run-decision__table {
    min-width: 520px;
  }

  /* ---------- 审批队列（现场镜头核心） ---------- */
  /* 桌面端把 ID / 徽标 / 元信息塞在一行靠 flex-wrap 兜底，375px 上会折成没有主次的
     碎行。这里统一改成竖向堆叠：卡片头两行（记录号 + 状态 / 风险级别独占一行），
     动作一行，运行 / 申请人 / 时间各占一行。 */
  .approval {
    padding: 12px;
  }

  .approval__head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 6px 8px;
  }

  .approval__head > .mono {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* 状态徽标在上排右侧，去掉它自带的左外边距以免栅格列里再缩进一次 */
  .approval__head .agents__chip {
    margin-left: 0;
  }

  /* 风险级别独占一行并靠左；flex 行里它最容易被压扁成看不清颜色的细条 */
  .approval__risk {
    grid-column: 1 / -1;
    justify-self: start;
    flex: 0 0 auto;
  }

  .approval__body {
    margin-top: 10px;
  }

  .approval__action {
    font-size: 14px;
    line-height: 1.5;
  }

  /* 运行 / 申请人 / 时间各占一行：不再拼成"半句 + 半句" */
  .approval__meta {
    flex-direction: column;
    gap: 4px;
    margin-top: 7px;
    font-size: 12.5px;
  }

  /* 批准 / 拒绝等分撑满宽度：录屏里手指落点会遮住按钮边缘，
     等分宽按钮让"批准"二字始终落在可见区域中点；44px 行高让手抖也点得中。 */
  .approval__actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin-top: 12px;
  }

  .approval__actions > .btn {
    width: 100%;
    min-width: 0;
    min-height: 44px;
    font-size: 15px;
  }

  /* 无审批权限的提示横跨整行，不跟按钮挤在同一格 */
  .approval__hint {
    grid-column: 1 / -1;
  }
}

@media (max-width: 430px) {
  /* 演示机型落在这个区间（375–430px）：再收一档内边距，
     省下的横向宽度直接加给按钮与摘要文本。 */
  .replay-bar,
  .steps,
  .run-detail__head,
  .run-detail__meta {
    padding-inline: 10px;
  }

  .replay-caption,
  .run-decision {
    margin-inline: 10px;
  }

  .approvals {
    padding-inline: 10px;
  }

  .approval {
    padding: 11px 10px;
  }
}
</style>
