<template>
  <div class="knowledge">
    <div class="knowledge__head">
      <div class="knowledge__head-copy">
        <h2 class="knowledge__title">领域资产认知智能体</h2>
        <p class="knowledge__sub">资产登记、本体演化、多跳检索与决策证据链</p>
      </div>
      <button class="btn" :disabled="refreshing" @click="refreshAll">
        {{ refreshing ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <div v-if="notice.text" class="knowledge__notice" :class="`knowledge__notice--${notice.type}`">
      <span>{{ notice.text }}</span>
      <button type="button" aria-label="关闭提示" @click="clearNotice">×</button>
    </div>

    <div class="knowledge__summary">
      <div class="knowledge__summary-item">
        <span>已提交资产</span>
        <strong>{{ assetMeta.total }}</strong>
      </div>
      <div class="knowledge__summary-item">
        <span>本体提交</span>
        <strong>{{ ontologyVersions.length }}</strong>
      </div>
      <div class="knowledge__summary-item">
        <span>已发布本体</span>
        <strong>{{ publishedOntologyCount }}</strong>
      </div>
      <div class="knowledge__summary-item">
        <span>决策提交</span>
        <strong>{{ decisionMeta.total }}</strong>
      </div>
      <div class="knowledge__summary-item">
        <span>检索命中</span>
        <strong>{{ searchResult?.total || 0 }}</strong>
      </div>
    </div>

    <div class="knowledge__tabs" role="tablist" aria-label="知识智能体功能">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        role="tab"
        :aria-selected="activeTab === tab.key"
        :class="{ 'knowledge__tab--active': activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- 资产 -->
    <template v-if="activeTab === 'assets'">
      <section class="panel">
        <div class="panel-title">
          <span>知识资产</span>
          <div class="knowledge__actions">
            <button class="btn" @click="loadAssets(assetMeta.page)">刷新</button>
            <button v-if="canWriteOps" class="btn btn--primary" @click="showAssetForm = !showAssetForm">
              {{ showAssetForm ? '收起登记' : '＋ 登记资产' }}
            </button>
          </div>
        </div>

        <div class="knowledge__toolbar">
          <input
            v-model.trim="assetFilters.query"
            class="form-input"
            type="search"
            placeholder="搜索标题、编号或描述"
            @keyup.enter="loadAssets(1)"
          />
          <select v-model="assetFilters.asset_type" class="form-input" @change="loadAssets(1)">
            <option value="">全部类型</option>
            <option v-for="item in ASSET_TYPES" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
          <select v-model="assetFilters.status" class="form-input" @change="loadAssets(1)">
            <option value="">全部状态</option>
            <option value="active">有效</option>
            <option value="draft">草稿</option>
            <option value="archived">归档</option>
          </select>
          <button class="btn" @click="loadAssets(1)">查询</button>
        </div>

        <form v-if="showAssetForm" class="knowledge__form" @submit.prevent="submitAsset">
          <div class="knowledge__form-grid">
            <label class="form-row">
              <span class="form-label">资产类型</span>
              <select v-model="assetForm.asset_type" class="form-input">
                <option v-for="item in ASSET_TYPES" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
            </label>
            <label class="form-row">
              <span class="form-label">标题</span>
              <input v-model.trim="assetForm.title" class="form-input" type="text" required />
            </label>
            <label class="form-row">
              <span class="form-label">来源系统</span>
              <input v-model.trim="assetForm.source_system" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">来源地址</span>
              <input v-model.trim="assetForm.source_uri" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">区域</span>
              <input v-model.trim="assetForm.region" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">乡镇</span>
              <input v-model.trim="assetForm.township" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">密级</span>
              <select v-model="assetForm.security_level" class="form-input">
                <option value="public">公开</option>
                <option value="internal">内部</option>
                <option value="restricted">受限</option>
              </select>
            </label>
            <label class="form-row">
              <span class="form-label">标准号（逗号分隔）</span>
              <input v-model.trim="assetForm.standard_codes" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">标签（逗号分隔）</span>
              <input v-model.trim="assetForm.tags" class="form-input" type="text" />
            </label>
            <label class="form-row knowledge__form-wide">
              <span class="form-label">描述</span>
              <input v-model.trim="assetForm.description" class="form-input" type="text" />
            </label>
            <label class="form-row knowledge__form-wide">
              <span class="form-label">初始正文</span>
              <textarea v-model="assetForm.content_text" class="form-input knowledge__textarea"></textarea>
            </label>
            <label class="form-row knowledge__form-wide">
              <span class="form-label">结构化内容 JSON</span>
              <textarea
                v-model.trim="assetForm.content_json"
                class="form-input knowledge__textarea knowledge__textarea--code"
                placeholder='{"metric": "value"}'
              ></textarea>
            </label>
          </div>
          <div class="knowledge__actions knowledge__actions--end">
            <button class="btn" type="button" @click="showAssetForm = false">取消</button>
            <button class="btn btn--primary" type="submit" :disabled="submittingAsset">
              {{ submittingAsset ? '登记中…' : '登记资产' }}
            </button>
          </div>
        </form>

        <div v-if="assetsLoading" class="knowledge__skeleton">
          <span v-for="i in 5" :key="i"></span>
        </div>
        <div v-else-if="assetsError" class="knowledge__error">
          <span>{{ assetsError }}</span>
          <button class="btn" @click="loadAssets(assetMeta.page)">重试</button>
        </div>
        <div v-else-if="!assets.length" class="empty">暂无知识资产</div>
        <div v-else class="table-scroll">
          <table class="data-table knowledge-table">
            <thead>
              <tr>
                <th>资产</th>
                <th>类型</th>
                <th>版本</th>
                <th>状态</th>
                <th>区域</th>
                <th>提交人</th>
                <th>更新时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="asset in assets" :key="asset.asset_id">
                <td>
                  <div class="knowledge__title-line">
                    <div class="knowledge__primary">{{ asset.title }}</div>
                    <span v-if="isDemoRecord(asset)" class="knowledge__demo">DEMO</span>
                  </div>
                  <div class="knowledge__id">{{ asset.asset_id }}</div>
                </td>
                <td>{{ assetTypeLabel(asset.asset_type) }}</td>
                <td class="knowledge__mono">v{{ asset.current_version }}</td>
                <td><span class="knowledge__pill">{{ assetStatusLabel(asset.status) }}</span></td>
                <td>{{ [asset.region, asset.township].filter(Boolean).join(' / ') || '—' }}</td>
                <td class="knowledge__muted">{{ asset.created_by || '—' }}</td>
                <td class="knowledge__muted">{{ fmtShortTime(asset.updated_at || asset.created_at) }}</td>
                <td>
                  <button class="knowledge__link" type="button" @click="openAsset(asset.asset_id)">
                    详情
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="assetMeta.total > assetMeta.page_size" class="knowledge__pager">
          <button class="btn" :disabled="assetMeta.page <= 1" @click="loadAssets(assetMeta.page - 1)">
            上一页
          </button>
          <span>第 {{ assetMeta.page }} / {{ assetPages }} 页 · 共 {{ assetMeta.total }} 条</span>
          <button
            class="btn"
            :disabled="assetMeta.page >= assetPages"
            @click="loadAssets(assetMeta.page + 1)"
          >
            下一页
          </button>
        </div>
      </section>

      <section v-if="selectedAsset" class="panel">
        <div class="panel-title">
          <span>资产详情 · {{ selectedAsset.asset.title }}</span>
          <div class="knowledge__actions">
            <button v-if="canWriteOps" class="btn" @click="showVersionForm = !showVersionForm">
              {{ showVersionForm ? '收起版本' : '＋ 追加版本' }}
            </button>
            <button class="btn" type="button" @click="selectedAsset = null">关闭</button>
          </div>
        </div>

        <div v-if="assetDetailLoading" class="knowledge__skeleton">
          <span v-for="i in 4" :key="i"></span>
        </div>
        <template v-else>
          <div class="knowledge__meta-grid">
            <div><span>资产编号</span><strong class="knowledge__mono">{{ selectedAsset.asset.asset_id }}</strong></div>
            <div><span>来源</span><strong>{{ selectedAsset.asset.source_system || '—' }}</strong></div>
            <div><span>提交人</span><strong>{{ selectedAsset.asset.created_by || '—' }}</strong></div>
            <div><span>提交时间</span><strong>{{ fmtShortTime(selectedAsset.asset.created_at) }}</strong></div>
            <div class="knowledge__meta-wide">
              <span>演示数据</span>
              <strong>{{ isDemoRecord(selectedAsset.asset) ? 'DEMO · 仅用于功能演示' : '否' }}</strong>
            </div>
            <div><span>标准号</span><strong>{{ selectedAsset.asset.standard_codes.join('、') || '—' }}</strong></div>
            <div><span>标签</span><strong>{{ selectedAsset.asset.tags.join('、') || '—' }}</strong></div>
            <div class="knowledge__meta-wide">
              <span>来源地址</span>
              <strong class="knowledge__mono">{{ selectedAsset.asset.source_uri || '—' }}</strong>
            </div>
          </div>

          <form v-if="showVersionForm" class="knowledge__form" @submit.prevent="submitVersion">
            <div class="knowledge__form-grid">
              <label class="form-row knowledge__form-wide">
                <span class="form-label">版本正文</span>
                <textarea v-model="versionForm.content_text" class="form-input knowledge__textarea"></textarea>
              </label>
              <label class="form-row">
                <span class="form-label">抽取方式</span>
                <input v-model.trim="versionForm.extraction_method" class="form-input" type="text" />
              </label>
              <label class="form-row">
                <span class="form-label">语言</span>
                <input v-model.trim="versionForm.language" class="form-input" type="text" />
              </label>
            </div>
            <div class="knowledge__actions knowledge__actions--end">
              <button class="btn" type="button" @click="showVersionForm = false">取消</button>
              <button class="btn btn--primary" type="submit" :disabled="submittingVersion">
                {{ submittingVersion ? '追加中…' : '追加版本' }}
              </button>
            </div>
          </form>

          <div v-if="!selectedAsset.versions.length" class="empty">暂无版本</div>
          <div v-else class="table-scroll">
            <table class="data-table knowledge-table">
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>抽取方式</th>
                  <th>语言</th>
                  <th>内容哈希</th>
                  <th>创建时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="version in selectedAsset.versions" :key="version.version_id">
                  <td>
                    <div class="knowledge__mono">v{{ version.version_no }}</div>
                    <div class="knowledge__id">{{ version.version_id }}</div>
                  </td>
                  <td>{{ versionStatusLabel(version.status) }}</td>
                  <td>{{ version.extraction_method }}</td>
                  <td>{{ version.language || '—' }}</td>
                  <td class="knowledge__mono knowledge__hash">{{ version.content_hash }}</td>
                  <td class="knowledge__muted">{{ fmtShortTime(version.created_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </section>
    </template>

    <!-- 视觉实测 -->
    <template v-else-if="activeTab === 'vision'">
      <section class="panel knowledge-vision">
        <div class="panel-title">
          <span>AI 视觉实测样例</span>
          <span class="knowledge-vision__engine">YOLO-World v2 · 4K</span>
        </div>

        <div class="knowledge-vision__intro">
          <div>
            <strong>真实开放词汇检测输出</strong>
            <p>照片来自公开许可素材，检测框由本机模型推理生成，未经手工修改。</p>
          </div>
          <span v-if="visionSamples.length" class="knowledge__pill">
            {{ visionSamples.length }} 个场景 · {{ visionDetectionTotal }} 个候选框
          </span>
        </div>

        <div v-if="visionLoading" class="knowledge__skeleton">
          <span v-for="i in 4" :key="i"></span>
        </div>
        <div v-else-if="visionError" class="knowledge__error">
          <span>{{ visionError }}</span>
          <button class="btn" @click="loadVisionBenchmark">重试</button>
        </div>
        <div v-else class="knowledge-vision__grid">
          <article v-for="sample in visionSamples" :key="sample.id" class="knowledge-vision__sample">
            <button
              type="button"
              class="knowledge-vision__preview"
              :style="{ aspectRatio: `${sample.item.width} / ${sample.item.height}` }"
              :aria-label="`在图片分析中查看${sample.title}`"
              @click="openVisionSample(sample)"
            >
              <img
                :src="visionAssetUrl(sample.image)"
                :alt="sample.title"
                width="3840"
                height="2560"
                loading="lazy"
                decoding="async"
              />
              <span
                v-for="(detection, index) in sample.item.detections"
                :key="index"
                class="knowledge-vision__box"
                :style="visionBoxStyle(detection, sample.item)"
              >
                <span
                  class="knowledge-vision__box-label"
                  :style="{ background: visionClassColor(detection.class) }"
                >
                  {{ visionClassLabel(detection.class) }}
                  {{ Math.round(detection.confidence * 1000) / 10 }}%
                </span>
              </span>
              <span class="knowledge-vision__count">
                {{ sample.item.count }} 个候选框
              </span>
            </button>

            <div class="knowledge-vision__body">
              <div class="knowledge-vision__title-row">
                <strong>{{ sample.title }}</strong>
                <span
                  class="knowledge__pill"
                  :class="{ 'knowledge__pill--in_review': sample.status === 'review' }"
                >
                  {{ sample.statusLabel }}
                </span>
              </div>
              <p>{{ sample.summary }}</p>
              <div class="knowledge-vision__metrics">
                <span>{{ sample.category }}</span>
                <span>{{ sample.item.width }}×{{ sample.item.height }}</span>
                <span>{{ sample.item.model }}</span>
                <span>{{ visionConfidenceRange(sample.item) }}</span>
              </div>
              <p class="knowledge-vision__review">{{ sample.reviewText }}</p>
              <div class="knowledge-vision__source">
                <span>{{ sample.source.author }} · {{ sample.source.license }}</span>
                <span>原图 {{ sample.source.originalSize }}</span>
              </div>
              <div class="knowledge-vision__actions">
                <a
                  :href="sample.source.pageUrl"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  查看素材与许可
                </a>
                <button type="button" @click="openVisionSample(sample)">
                  查看大图与检测框
                </button>
              </div>
            </div>
          </article>
        </div>

        <p class="knowledge-vision__disclaimer">
          测试照片不代表连江本地实拍；低置信度候选仅用于展示模型边界，治理处置前应经过人工复核。
        </p>
      </section>
    </template>

    <!-- 本体 -->
    <template v-else-if="activeTab === 'ontology'">
      <div class="knowledge__grid">
        <section class="panel">
          <div class="panel-title">
            <span>本体版本</span>
            <button v-if="canWriteOps" class="btn" @click="showOntologyForm = !showOntologyForm">
              {{ showOntologyForm ? '收起新建' : '＋ 新建' }}
            </button>
          </div>
          <form v-if="showOntologyForm" class="knowledge__form" @submit.prevent="submitOntology">
            <div class="knowledge__form-grid">
              <label class="form-row">
                <span class="form-label">本体名称</span>
                <input v-model.trim="ontologyForm.name" class="form-input" type="text" required />
              </label>
              <label class="form-row">
                <span class="form-label">标准号（逗号分隔）</span>
                <input v-model.trim="ontologyForm.standard_codes" class="form-input" type="text" />
              </label>
              <label class="form-row knowledge__form-wide">
                <span class="form-label">说明</span>
                <input v-model.trim="ontologyForm.description" class="form-input" type="text" />
              </label>
            </div>
            <div class="knowledge__actions knowledge__actions--end">
              <button class="btn" type="button" @click="showOntologyForm = false">取消</button>
              <button class="btn btn--primary" type="submit" :disabled="submittingOntology">
                {{ submittingOntology ? '创建中…' : '创建本体' }}
              </button>
            </div>
          </form>

          <div v-if="ontologyLoading && !ontologyVersions.length" class="knowledge__skeleton">
            <span v-for="i in 5" :key="i"></span>
          </div>
          <div v-else-if="ontologyError" class="knowledge__error">
            <span>{{ ontologyError }}</span>
            <button class="btn" @click="loadOntologyVersions">重试</button>
          </div>
          <div v-else-if="!ontologyVersions.length" class="empty">暂无本体版本</div>
          <div v-else class="knowledge__list">
            <button
              v-for="version in ontologyVersions"
              :key="version.version_id"
              type="button"
              class="knowledge__list-item"
              :class="{ 'knowledge__list-item--active': selectedOntologyId === version.version_id }"
              @click="selectOntology(version.version_id)"
            >
              <span>
                <strong>{{ version.name }} · v{{ version.version_no }}</strong>
                <small>
                  {{ version.version_id }} · {{ version.created_by || '—' }} ·
                  {{ fmtShortTime(version.created_at) }}
                </small>
              </span>
              <span class="knowledge__row-tail">
                <span v-if="isDemoRecord(version)" class="knowledge__demo">DEMO</span>
                <span class="knowledge__pill" :class="`knowledge__pill--${version.status}`">
                  {{ ontologyStatusLabel(version.status) }}
                </span>
              </span>
            </button>
          </div>
        </section>

        <section class="panel">
          <div class="panel-title">
            <span>本体审核与发布</span>
            <span v-if="selectedOntology" class="knowledge__pill">{{ ontologyStatusLabel(selectedOntology.status) }}</span>
          </div>

          <div v-if="!selectedOntology" class="empty">请选择一个本体版本</div>
          <template v-else>
            <form v-if="canWriteOps" class="knowledge__form" @submit.prevent="submitExtract">
              <div class="knowledge__form-grid knowledge__form-grid--three">
                <label class="form-row">
                  <span class="form-label">来源资产</span>
                  <select v-model="extractForm.asset_id" class="form-input" @change="onExtractAssetChange">
                    <option value="">选择资产</option>
                    <option v-for="asset in assets" :key="asset.asset_id" :value="asset.asset_id">
                      {{ asset.title }}
                    </option>
                  </select>
                </label>
                <label class="form-row">
                  <span class="form-label">资产版本</span>
                  <select v-model="extractForm.version_id" class="form-input">
                    <option value="">选择版本</option>
                    <option v-for="version in extractVersions" :key="version.version_id" :value="version.version_id">
                      v{{ version.version_no }} · {{ version.status }}
                    </option>
                  </select>
                </label>
                <label class="form-row">
                  <span class="form-label">最大节点</span>
                  <input v-model.number="extractForm.max_nodes" class="form-input" type="number" min="1" max="100" />
                </label>
                <label class="form-row">
                  <span class="form-label">最大关系</span>
                  <input v-model.number="extractForm.max_relations" class="form-input" type="number" min="0" max="200" />
                </label>
              </div>
              <div class="knowledge__actions knowledge__actions--end">
                <button class="btn btn--primary" type="submit" :disabled="extracting">
                  {{ extracting ? '抽取中…' : '抽取候选' }}
                </button>
              </div>
            </form>

            <div class="knowledge__review-bar">
              <input
                v-model.trim="reviewReason"
                class="form-input"
                type="text"
                placeholder="审核意见（可选）"
              />
              <button
                v-if="canReview"
                class="btn btn--primary"
                type="button"
                :disabled="publishing"
                @click="publishSelectedOntology"
              >
                {{ publishing ? '发布中…' : '发布本体' }}
              </button>
              <span v-else class="knowledge__muted">审核与发布需要 admin 或 approver</span>
            </div>

            <div v-if="ontologyDetailLoading" class="knowledge__skeleton">
              <span v-for="i in 6" :key="i"></span>
            </div>
            <div v-else-if="ontologyDetailError" class="knowledge__error">
              <span>{{ ontologyDetailError }}</span>
              <button class="btn" @click="selectOntology(selectedOntologyId)">重试</button>
            </div>
            <template v-else>
              <div class="knowledge__section-head">
                <strong>节点</strong>
                <span>{{ ontologyNodes.length }}</span>
              </div>
              <div v-if="!ontologyNodes.length" class="empty">暂无节点候选</div>
              <div v-else class="table-scroll">
                <table class="data-table knowledge-table knowledge-table--ontology">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>置信度</th>
                      <th>状态</th>
                      <th>来源</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="node in ontologyNodes" :key="node.node_id">
                      <td>
                        <div class="knowledge__primary">{{ node.name }}</div>
                        <div class="knowledge__id">{{ node.node_id }}</div>
                      </td>
                      <td>{{ node.entity_type }}</td>
                      <td class="knowledge__mono">{{ formatScore(node.confidence) }}</td>
                      <td><span class="knowledge__pill">{{ reviewStatusLabel(node.review_status) }}</span></td>
                      <td class="knowledge__id">{{ node.source_version_id || '—' }}</td>
                      <td>
                        <div v-if="canReview && node.review_status === 'proposed'" class="knowledge__row-actions">
                          <button class="knowledge__link" type="button" @click="reviewNode(node, 'approved')">通过</button>
                          <button class="knowledge__link knowledge__link--danger" type="button" @click="reviewNode(node, 'rejected')">拒绝</button>
                        </div>
                        <span v-else class="knowledge__muted">—</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <div class="knowledge__section-head">
                <strong>关系</strong>
                <span>{{ ontologyRelations.length }}</span>
              </div>
              <div v-if="!ontologyRelations.length" class="empty">暂无关系候选</div>
              <div v-else class="table-scroll">
                <table class="data-table knowledge-table knowledge-table--relations">
                  <thead>
                    <tr>
                      <th>关系</th>
                      <th>源节点</th>
                      <th>目标节点</th>
                      <th>置信度</th>
                      <th>状态</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="relation in ontologyRelations" :key="relation.relation_id">
                      <td>
                        <div class="knowledge__primary">{{ relation.relation_type }}</div>
                        <div class="knowledge__id">{{ relation.relation_id }}</div>
                      </td>
                      <td class="knowledge__id">{{ relation.source_node_id }}</td>
                      <td class="knowledge__id">{{ relation.target_node_id }}</td>
                      <td class="knowledge__mono">{{ formatScore(relation.confidence) }}</td>
                      <td><span class="knowledge__pill">{{ reviewStatusLabel(relation.review_status) }}</span></td>
                      <td>
                        <div v-if="canReview && relation.review_status === 'proposed'" class="knowledge__row-actions">
                          <button class="knowledge__link" type="button" @click="reviewRelation(relation, 'approved')">通过</button>
                          <button class="knowledge__link knowledge__link--danger" type="button" @click="reviewRelation(relation, 'rejected')">拒绝</button>
                        </div>
                        <span v-else class="knowledge__muted">—</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </template>
        </section>
      </div>
    </template>

    <!-- 检索 -->
    <template v-else-if="activeTab === 'search'">
      <section class="panel">
        <div class="panel-title">
          <span>跨文档多跳检索</span>
          <span v-if="searchResult" class="knowledge__pill">
            {{ searchResult.mode === 'ontology_graph' ? '本体图检索' : '关键词检索' }}
          </span>
        </div>
        <div class="knowledge__examples">
          <span>演示问题</span>
          <button
            v-for="example in SEARCH_EXAMPLES"
            :key="example"
            type="button"
            :disabled="searching"
            @click="applySearchExample(example)"
          >
            {{ example }}
          </button>
        </div>
        <form class="knowledge__form" @submit.prevent="submitSearch">
          <div class="knowledge__form-grid knowledge__form-grid--three">
            <label class="form-row knowledge__form-wide">
              <span class="form-label">问题或关键词</span>
              <input v-model.trim="searchForm.query" class="form-input" type="text" required />
            </label>
            <label class="form-row">
              <span class="form-label">本体版本</span>
              <select v-model="searchForm.ontology_version_id" class="form-input">
                <option value="">自动选择已发布本体</option>
                <option v-for="version in publishedOntologies" :key="version.version_id" :value="version.version_id">
                  {{ version.name }} · v{{ version.version_no }}
                </option>
              </select>
            </label>
            <label class="form-row">
              <span class="form-label">跳数</span>
              <select v-model.number="searchForm.hop_depth" class="form-input">
                <option :value="1">1 跳</option>
                <option :value="2">2 跳</option>
                <option :value="3">3 跳</option>
              </select>
            </label>
            <label class="form-row">
              <span class="form-label">返回条数</span>
              <input v-model.number="searchForm.limit" class="form-input" type="number" min="1" max="50" />
            </label>
            <label class="form-row knowledge__form-wide">
              <span class="form-label">标准号筛选（逗号分隔）</span>
              <input v-model.trim="searchForm.standard_codes" class="form-input" type="text" />
            </label>
          </div>
          <div class="knowledge__actions knowledge__actions--end">
            <button class="btn btn--primary" type="submit" :disabled="searching">
              {{ searching ? '检索中…' : '检索' }}
            </button>
          </div>
        </form>

        <div v-if="searchError" class="knowledge__error">
          <span>{{ searchError }}</span>
        </div>
        <div v-else-if="searching" class="knowledge__skeleton">
          <span v-for="i in 4" :key="i"></span>
        </div>
        <div v-else-if="!searchResult" class="empty">尚未执行检索</div>
        <div v-else-if="!searchResult.results.length" class="empty">没有命中结果</div>
        <div v-else class="knowledge__results">
          <article v-for="hit in searchResult.results" :key="hit.asset_version_id" class="knowledge__result">
            <div class="knowledge__result-head">
              <strong>{{ hit.title }}</strong>
              <span>{{ assetTypeLabel(hit.asset_type) }} · {{ hit.hop_count }} 跳</span>
            </div>
            <p>{{ hit.snippet || '—' }}</p>
            <div class="knowledge__id">{{ hit.asset_id }} / {{ hit.asset_version_id }}</div>
            <div v-if="hit.path.length" class="knowledge__path">
              <span v-for="step in hit.path" :key="`${hit.asset_version_id}-${step.hop_no}`">
                {{ step.hop_no }}. {{ step.relation_type || 'relation' }}
              </span>
            </div>
            <div v-if="hit.citations.length" class="knowledge__citations">
              <div v-for="(citation, index) in hit.citations" :key="`${hit.asset_version_id}-citation-${index}`">
                {{ citation }}
              </div>
            </div>
          </article>
        </div>
      </section>
    </template>

    <!-- 决策 -->
    <template v-else>
      <section class="panel">
        <div class="panel-title">
          <span>决策轨迹</span>
          <button v-if="canWriteOps" class="btn btn--primary" @click="showDecisionForm = !showDecisionForm">
            {{ showDecisionForm ? '收起记录' : '＋ 记录决策' }}
          </button>
        </div>

        <form v-if="showDecisionForm" class="knowledge__form" @submit.prevent="submitDecision">
          <div class="knowledge__form-grid knowledge__form-grid--three">
            <label class="form-row knowledge__form-wide">
              <span class="form-label">决策问题</span>
              <input v-model.trim="decisionForm.question" class="form-input" type="text" required />
            </label>
            <label class="form-row knowledge__form-wide">
              <span class="form-label">结论摘要</span>
              <textarea v-model.trim="decisionForm.answer_summary" class="form-input knowledge__textarea"></textarea>
            </label>
            <label class="form-row">
              <span class="form-label">本体版本</span>
              <select v-model="decisionForm.ontology_version_id" class="form-input">
                <option value="">自动检索</option>
                <option v-for="version in publishedOntologies" :key="version.version_id" :value="version.version_id">
                  {{ version.name }} · v{{ version.version_no }}
                </option>
              </select>
            </label>
            <label class="form-row">
              <span class="form-label">策略版本</span>
              <input v-model.trim="decisionForm.policy_version" class="form-input" type="text" />
            </label>
            <label class="form-row">
              <span class="form-label">跳数</span>
              <select v-model.number="decisionForm.hop_depth" class="form-input">
                <option :value="1">1 跳</option>
                <option :value="2">2 跳</option>
                <option :value="3">3 跳</option>
              </select>
            </label>
          </div>
          <div class="knowledge__actions knowledge__actions--end">
            <button class="btn" type="button" @click="showDecisionForm = false">取消</button>
            <button class="btn btn--primary" type="submit" :disabled="submittingDecision">
              {{ submittingDecision ? '生成中…' : '生成决策与证据链' }}
            </button>
          </div>
        </form>

        <div v-if="decisionsLoading" class="knowledge__skeleton">
          <span v-for="i in 5" :key="i"></span>
        </div>
        <div v-else-if="decisionsError" class="knowledge__error">
          <span>{{ decisionsError }}</span>
          <button class="btn" @click="loadDecisions(decisionMeta.page)">重试</button>
        </div>
        <div v-else-if="!decisions.length" class="empty">暂无决策轨迹</div>
        <div v-else class="knowledge__list">
          <button
            v-for="trace in decisions"
            :key="trace.trace_id"
            type="button"
            class="knowledge__list-item"
            :class="{ 'knowledge__list-item--active': selectedTraceId === trace.trace_id }"
            @click="openDecision(trace.trace_id)"
          >
            <span>
              <strong>{{ trace.question }}</strong>
              <small>
                {{ trace.trace_id }} · {{ trace.created_by || '—' }} ·
                {{ fmtShortTime(trace.created_at) }}
              </small>
            </span>
            <span class="knowledge__row-tail">
              <span v-if="isDemoRecord(trace)" class="knowledge__demo">DEMO</span>
              <span class="knowledge__pill">{{ decisionStatusLabel(trace.status) }}</span>
            </span>
          </button>
        </div>

        <div v-if="decisionMeta.total > decisionMeta.page_size" class="knowledge__pager">
          <button class="btn" :disabled="decisionMeta.page <= 1" @click="loadDecisions(decisionMeta.page - 1)">
            上一页
          </button>
          <span>第 {{ decisionMeta.page }} / {{ decisionPages }} 页 · 共 {{ decisionMeta.total }} 条</span>
          <button
            class="btn"
            :disabled="decisionMeta.page >= decisionPages"
            @click="loadDecisions(decisionMeta.page + 1)"
          >
            下一页
          </button>
        </div>
      </section>

      <section v-if="selectedTrace" class="panel">
        <div class="panel-title">
          <span>证据链 · {{ selectedTrace.trace_id }}</span>
          <button class="btn" type="button" @click="selectedTrace = null">关闭</button>
        </div>
        <div class="knowledge__meta-grid">
          <div class="knowledge__meta-wide">
            <span>问题</span>
            <strong>{{ selectedTrace.question }}</strong>
          </div>
          <div class="knowledge__meta-wide">
            <span>结论摘要</span>
            <strong>{{ selectedTrace.answer_summary || '—' }}</strong>
          </div>
          <div><span>状态</span><strong>{{ decisionStatusLabel(selectedTrace.status) }}</strong></div>
          <div><span>本体版本</span><strong class="knowledge__mono">{{ selectedTrace.ontology_version_id || '—' }}</strong></div>
          <div><span>提交人</span><strong>{{ selectedTrace.created_by || '—' }}</strong></div>
          <div><span>提交时间</span><strong>{{ fmtShortTime(selectedTrace.created_at) }}</strong></div>
        </div>
        <div v-if="evidenceLoading" class="knowledge__skeleton">
          <span v-for="i in 4" :key="i"></span>
        </div>
        <div v-else-if="evidenceError" class="knowledge__error">
          <span>{{ evidenceError }}</span>
          <button class="btn" @click="openDecision(selectedTrace.trace_id)">重试</button>
        </div>
        <div v-else-if="!decisionEvidence.length" class="empty">证据不足，未形成可引用链条</div>
        <div v-else class="knowledge__evidence">
          <article v-for="item in decisionEvidence" :key="item.evidence_id" class="knowledge__evidence-item">
            <span class="knowledge__evidence-rank">{{ item.rank_no }}</span>
            <div>
              <p>{{ item.citation_text }}</p>
              <div class="knowledge__result-meta">
                <span>资产 {{ item.asset_id || '—' }}</span>
                <span>版本 {{ item.asset_version_id || '—' }}</span>
                <span>{{ item.hop_no }} 跳</span>
                <span>评分 {{ formatScore(item.score) }}</span>
              </div>
            </div>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { knowledgeApi } from '@/api'
import { canWrite, getUser } from '@/utils/auth'
import { fmtShortTime } from '@/utils/format'
import { VISION_SAMPLES, loadVisionSample, visionAssetUrl } from '@/utils/visionSamples'

const TABS = [
  { key: 'assets', label: '资产' },
  { key: 'vision', label: '视觉实测' },
  { key: 'ontology', label: '本体' },
  { key: 'search', label: '检索' },
  { key: 'decisions', label: '决策' },
]

const ASSET_TYPES = [
  { value: 'document', label: '文档' },
  { value: 'table', label: '表格' },
  { value: 'image', label: '图文' },
  { value: 'event', label: '事件' },
  { value: 'telemetry', label: '遥测' },
  { value: 'dataset', label: '数据集' },
]

const SEARCH_EXAMPLES = [
  '马鼻镇发现泡沫塑料后应如何处置？',
  '黄岐渔港出现废弃渔具，应安排哪个流程？',
  '近 24 小时泡沫垃圾聚集与风向有什么关系？',
]

const activeTab = ref('assets')
const router = useRouter()
const canWriteOps = canWrite()
const canReview = ['admin', 'approver'].includes(getUser()?.role)
const refreshing = ref(false)
const notice = reactive({ type: 'ok', text: '' })

const visionSamples = ref([])
const visionLoading = ref(false)
const visionError = ref('')

const assets = ref([])
const assetMeta = reactive({ total: 0, page: 1, page_size: 20 })
const assetFilters = reactive({ query: '', asset_type: '', status: '' })
const assetsLoading = ref(false)
const assetsError = ref('')
const showAssetForm = ref(false)
const submittingAsset = ref(false)
const assetForm = reactive({
  asset_type: 'document',
  title: '',
  description: '',
  source_uri: '',
  source_system: '',
  region: '',
  township: '',
  security_level: 'internal',
  standard_codes: '',
  tags: '',
  content_text: '',
  content_json: '',
})

const selectedAsset = ref(null)
const assetDetailLoading = ref(false)
const showVersionForm = ref(false)
const submittingVersion = ref(false)
const versionForm = reactive({
  content_text: '',
  extraction_method: 'manual',
  language: 'zh-CN',
})

const ontologyVersions = ref([])
const selectedOntologyId = ref('')
const ontologyNodes = ref([])
const ontologyRelations = ref([])
const ontologyLoading = ref(false)
const ontologyDetailLoading = ref(false)
const ontologyError = ref('')
const ontologyDetailError = ref('')
const showOntologyForm = ref(false)
const submittingOntology = ref(false)
const ontologyForm = reactive({ name: '', description: '', standard_codes: '' })
const extracting = ref(false)
const publishing = ref(false)
const reviewReason = ref('')
const extractVersions = ref([])
const extractForm = reactive({
  asset_id: '',
  version_id: '',
  max_nodes: 20,
  max_relations: 40,
})

const searchForm = reactive({
  query: '',
  ontology_version_id: '',
  hop_depth: 2,
  limit: 10,
  standard_codes: '',
})
const searching = ref(false)
const searchError = ref('')
const searchResult = ref(null)

const decisions = ref([])
const decisionMeta = reactive({ total: 0, page: 1, page_size: 20 })
const decisionsLoading = ref(false)
const decisionsError = ref('')
const showDecisionForm = ref(false)
const submittingDecision = ref(false)
const decisionForm = reactive({
  question: '',
  answer_summary: '',
  ontology_version_id: '',
  policy_version: '',
  hop_depth: 2,
})
const selectedTrace = ref(null)
const selectedTraceId = ref('')
const decisionEvidence = ref([])
const evidenceLoading = ref(false)
const evidenceError = ref('')

const assetPages = computed(() => Math.max(1, Math.ceil(assetMeta.total / assetMeta.page_size)))
const decisionPages = computed(() => Math.max(1, Math.ceil(decisionMeta.total / decisionMeta.page_size)))
const publishedOntologies = computed(() =>
  ontologyVersions.value.filter((version) => version.status === 'published'),
)
const publishedOntologyCount = computed(() => publishedOntologies.value.length)
const visionDetectionTotal = computed(() =>
  visionSamples.value.reduce((total, sample) => total + Number(sample.item?.count || 0), 0),
)
const selectedOntology = computed(() =>
  ontologyVersions.value.find((version) => version.version_id === selectedOntologyId.value) || null,
)

const VISION_CLASSES = {
  'plastic bottle': { label: '塑料瓶', color: '#007aff' },
  shoe: { label: '鞋类', color: '#5856d6' },
  'fishing net': { label: '渔网', color: '#ff3b30' },
  'fishing gear': { label: '渔具', color: '#ff3b30' },
  'polystyrene cup': { label: '聚苯乙烯杯', color: '#ff9500' },
  'foam cup': { label: '泡沫杯', color: '#ff9500' },
}

function visionClassMeta(value) {
  return VISION_CLASSES[value] || { label: value || '目标', color: '#8e8e93' }
}

function visionClassLabel(value) {
  return visionClassMeta(value).label
}

function visionClassColor(value) {
  return visionClassMeta(value).color
}

function visionBoxStyle(detection, item) {
  const width = Number(item?.width || 1)
  const height = Number(item?.height || 1)
  const [x1, y1, x2, y2] = detection.bbox || [0, 0, 0, 0]
  return {
    left: `${(Number(x1) / width) * 100}%`,
    top: `${(Number(y1) / height) * 100}%`,
    width: `${((Number(x2) - Number(x1)) / width) * 100}%`,
    height: `${((Number(y2) - Number(y1)) / height) * 100}%`,
    borderColor: visionClassColor(detection.class),
  }
}

function visionConfidenceRange(item) {
  const values = (item?.detections || []).map((detection) => Number(detection.confidence || 0))
  if (!values.length) return '无候选框'
  const min = Math.min(...values)
  const max = Math.max(...values)
  const format = (value) => `${Math.round(value * 1000) / 10}%`
  return min === max ? `置信度 ${format(max)}` : `置信度 ${format(min)}–${format(max)}`
}

async function loadVisionBenchmark() {
  visionLoading.value = true
  visionError.value = ''
  try {
    visionSamples.value = await Promise.all(
      VISION_SAMPLES.map(async (sample) => ({
        ...sample,
        item: await loadVisionSample(sample),
      })),
    )
  } catch (error) {
    visionSamples.value = []
    visionError.value = error.message || '视觉样例加载失败'
  } finally {
    visionLoading.value = false
  }
}

function openVisionSample(sample) {
  router.push({ name: 'analyze', query: { sample: sample.id } })
}

function setNotice(text, type = 'ok') {
  notice.text = text
  notice.type = type
}

function clearNotice() {
  notice.text = ''
}

function parseCsv(value) {
  return String(value || '')
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseJson(value, label) {
  if (!String(value || '').trim()) return null
  try {
    return JSON.parse(value)
  } catch {
    throw new Error(`${label}不是有效 JSON`)
  }
}

function isDemoRecord(record) {
  if (!record || typeof record !== 'object') return false
  const tags = Array.isArray(record.tags) ? record.tags : []
  if (tags.some((tag) => String(tag).toUpperCase().includes('DEMO'))) return true
  const metadata = record.metadata ?? record.attributes ?? {}
  try {
    return JSON.stringify(metadata).toUpperCase().includes('DEMO')
  } catch {
    return false
  }
}

function assetTypeLabel(value) {
  return ASSET_TYPES.find((item) => item.value === value)?.label || value || '—'
}

function assetStatusLabel(value) {
  return { draft: '草稿', active: '有效', archived: '归档' }[value] || value || '—'
}

function versionStatusLabel(value) {
  return { active: '当前', superseded: '历史', archived: '归档' }[value] || value || '—'
}

function ontologyStatusLabel(value) {
  return {
    draft: '草稿',
    in_review: '审核中',
    published: '已发布',
    retired: '已停用',
  }[value] || value || '—'
}

function reviewStatusLabel(value) {
  return { proposed: '待审核', approved: '已通过', rejected: '已拒绝' }[value] || value || '—'
}

function decisionStatusLabel(value) {
  return { completed: '已完成', insufficient_evidence: '证据不足' }[value] || value || '—'
}

function formatScore(value) {
  return Number(value || 0).toFixed(3)
}

async function loadAssets(page = 1) {
  assetsLoading.value = true
  assetsError.value = ''
  try {
    const data = await knowledgeApi.listAssets({
      query: assetFilters.query || undefined,
      asset_type: assetFilters.asset_type || undefined,
      status: assetFilters.status || undefined,
      page,
      page_size: assetMeta.page_size,
    })
    assets.value = data?.items || []
    Object.assign(assetMeta, data?.meta || { total: 0, page, page_size: assetMeta.page_size })
  } catch (error) {
    assetsError.value = error.message
  } finally {
    assetsLoading.value = false
  }
}

async function openAsset(assetId) {
  assetDetailLoading.value = true
  try {
    selectedAsset.value = await knowledgeApi.assetDetail(assetId)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    assetDetailLoading.value = false
  }
}

async function submitAsset() {
  submittingAsset.value = true
  try {
    const contentJson = parseJson(assetForm.content_json, '结构化内容')
    await knowledgeApi.createAsset({
      asset_type: assetForm.asset_type,
      title: assetForm.title,
      description: assetForm.description || null,
      source_uri: assetForm.source_uri || null,
      source_system: assetForm.source_system || null,
      region: assetForm.region || null,
      township: assetForm.township || null,
      security_level: assetForm.security_level,
      standard_codes: parseCsv(assetForm.standard_codes),
      tags: parseCsv(assetForm.tags),
      attributes: {},
      initial_content: {
        content_text: assetForm.content_text || null,
        content_json: contentJson,
        extraction_method: 'manual',
      },
    })
    showAssetForm.value = false
    assetForm.title = ''
    assetForm.description = ''
    assetForm.content_text = ''
    assetForm.content_json = ''
    setNotice('知识资产已登记')
    await loadAssets(1)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    submittingAsset.value = false
  }
}

async function submitVersion() {
  if (!selectedAsset.value) return
  submittingVersion.value = true
  try {
    await knowledgeApi.appendAssetVersion(selectedAsset.value.asset.asset_id, {
      content_text: versionForm.content_text || null,
      content_json: null,
      extraction_method: versionForm.extraction_method || 'manual',
      language: versionForm.language || null,
    })
    versionForm.content_text = ''
    showVersionForm.value = false
    setNotice('资产版本已追加')
    await Promise.all([
      openAsset(selectedAsset.value.asset.asset_id),
      loadAssets(assetMeta.page),
    ])
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    submittingVersion.value = false
  }
}

async function loadOntologyVersions() {
  ontologyLoading.value = true
  ontologyError.value = ''
  try {
    ontologyVersions.value = await knowledgeApi.listOntologyVersions()
    const selectedExists = ontologyVersions.value.some(
      (version) => version.version_id === selectedOntologyId.value,
    )
    if (!selectedExists && ontologyVersions.value.length) {
      await selectOntology(ontologyVersions.value[0].version_id)
    }
  } catch (error) {
    ontologyError.value = error.message
  } finally {
    ontologyLoading.value = false
  }
}

async function selectOntology(versionId) {
  if (!versionId) return
  selectedOntologyId.value = versionId
  ontologyDetailLoading.value = true
  ontologyDetailError.value = ''
  try {
    const [nodes, relations] = await Promise.all([
      knowledgeApi.ontologyNodes(versionId),
      knowledgeApi.ontologyRelations(versionId),
    ])
    ontologyNodes.value = nodes || []
    ontologyRelations.value = relations || []
  } catch (error) {
    ontologyDetailError.value = error.message
    ontologyNodes.value = []
    ontologyRelations.value = []
  } finally {
    ontologyDetailLoading.value = false
  }
}

async function submitOntology() {
  submittingOntology.value = true
  try {
    const created = await knowledgeApi.createOntologyVersion({
      name: ontologyForm.name,
      description: ontologyForm.description || null,
      standard_codes: parseCsv(ontologyForm.standard_codes),
    })
    ontologyForm.name = ''
    ontologyForm.description = ''
    ontologyForm.standard_codes = ''
    showOntologyForm.value = false
    setNotice('本体版本已创建')
    await loadOntologyVersions()
    await selectOntology(created.version_id)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    submittingOntology.value = false
  }
}

async function onExtractAssetChange() {
  extractForm.version_id = ''
  extractVersions.value = []
  if (!extractForm.asset_id) return
  try {
    const detail = await knowledgeApi.assetDetail(extractForm.asset_id)
    extractVersions.value = detail?.versions || []
    if (extractVersions.value.length) {
      extractForm.version_id = extractVersions.value[0].version_id
    }
  } catch (error) {
    setNotice(error.message, 'error')
  }
}

async function submitExtract() {
  if (!extractForm.version_id) {
    setNotice('请先选择来源资产版本', 'error')
    return
  }
  extracting.value = true
  try {
    const result = await knowledgeApi.extractOntology({
      ontology_version_id: selectedOntologyId.value,
      asset_version_ids: [extractForm.version_id],
      max_nodes: extractForm.max_nodes,
      max_relations: extractForm.max_relations,
      min_term_length: 2,
    })
    setNotice(`候选已生成：${result.created_nodes} 个节点，${result.created_relations} 条关系`)
    await loadOntologyVersions()
    await selectOntology(selectedOntologyId.value)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    extracting.value = false
  }
}

async function reviewNode(node, decision) {
  try {
    await knowledgeApi.reviewNode(node.node_id, {
      decision,
      reason: reviewReason.value || (decision === 'approved' ? '人工复核通过' : '人工复核拒绝'),
    })
    setNotice(decision === 'approved' ? '节点已通过' : '节点已拒绝')
    await selectOntology(selectedOntologyId.value)
  } catch (error) {
    setNotice(error.message, 'error')
  }
}

async function reviewRelation(relation, decision) {
  try {
    await knowledgeApi.reviewRelation(relation.relation_id, {
      decision,
      reason: reviewReason.value || (decision === 'approved' ? '人工复核通过' : '人工复核拒绝'),
    })
    setNotice(decision === 'approved' ? '关系已通过' : '关系已拒绝')
    await selectOntology(selectedOntologyId.value)
  } catch (error) {
    setNotice(error.message, 'error')
  }
}

async function publishSelectedOntology() {
  if (!selectedOntologyId.value) return
  publishing.value = true
  try {
    const result = await knowledgeApi.publishOntology(selectedOntologyId.value)
    setNotice(
      `本体已发布：通过 ${result.approved_nodes} 个节点、${result.approved_relations} 条关系`,
    )
    await loadOntologyVersions()
    await selectOntology(selectedOntologyId.value)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    publishing.value = false
  }
}

async function submitSearch() {
  searching.value = true
  searchError.value = ''
  try {
    searchResult.value = await knowledgeApi.search({
      query: searchForm.query,
      ontology_version_id: searchForm.ontology_version_id || null,
      hop_depth: searchForm.hop_depth,
      asset_types: [],
      standard_codes: parseCsv(searchForm.standard_codes),
      limit: searchForm.limit,
    })
  } catch (error) {
    searchError.value = error.message
    searchResult.value = null
  } finally {
    searching.value = false
  }
}

function applySearchExample(query) {
  searchForm.query = query
  void submitSearch()
}

async function loadDecisions(page = 1) {
  decisionsLoading.value = true
  decisionsError.value = ''
  try {
    const data = await knowledgeApi.listDecisions({ page, page_size: decisionMeta.page_size })
    decisions.value = data?.items || []
    Object.assign(decisionMeta, data?.meta || { total: 0, page, page_size: decisionMeta.page_size })
  } catch (error) {
    decisionsError.value = error.message
  } finally {
    decisionsLoading.value = false
  }
}

async function submitDecision() {
  submittingDecision.value = true
  try {
    const trace = await knowledgeApi.createDecision({
      question: decisionForm.question,
      answer_summary: decisionForm.answer_summary || null,
      ontology_version_id: decisionForm.ontology_version_id || null,
      policy_version: decisionForm.policy_version || null,
      hop_depth: decisionForm.hop_depth,
      evidence: [],
      metadata: {},
    })
    decisionForm.question = ''
    decisionForm.answer_summary = ''
    showDecisionForm.value = false
    setNotice(trace.status === 'completed' ? '决策轨迹已形成' : '证据不足，轨迹已如实记录', trace.status === 'completed' ? 'ok' : 'warn')
    await loadDecisions(1)
    await openDecision(trace.trace_id)
  } catch (error) {
    setNotice(error.message, 'error')
  } finally {
    submittingDecision.value = false
  }
}

async function openDecision(traceId) {
  selectedTraceId.value = traceId
  selectedTrace.value = decisions.value.find((item) => item.trace_id === traceId) || null
  evidenceLoading.value = true
  evidenceError.value = ''
  try {
    decisionEvidence.value = await knowledgeApi.decisionEvidence(traceId)
    if (!decisionEvidence.value.length) {
      const data = await knowledgeApi.listDecisions({ page: 1, page_size: 200 })
      selectedTrace.value = data?.items?.find((item) => item.trace_id === traceId) || selectedTrace.value
    }
  } catch (error) {
    evidenceError.value = error.message
    decisionEvidence.value = []
  } finally {
    evidenceLoading.value = false
  }
}

async function refreshAll() {
  refreshing.value = true
  try {
    await Promise.all([
      loadAssets(assetMeta.page),
      loadOntologyVersions(),
      loadDecisions(decisionMeta.page),
    ])
  } finally {
    refreshing.value = false
  }
}

onMounted(() => {
  void refreshAll()
  void loadVisionBenchmark()
})
</script>

<style scoped>
.knowledge {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  letter-spacing: 0;
}

.knowledge__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.knowledge__head-copy {
  min-width: 0;
}

.knowledge__title {
  margin: 0;
  color: var(--text-main);
  font-size: 21px;
  line-height: 1.25;
}

.knowledge__sub {
  margin: 5px 0 0;
  color: var(--text-sub);
  font-size: 12.5px;
}

.knowledge__notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 42px;
  padding: 9px 12px;
  border: 1px solid rgba(52, 199, 89, 0.3);
  border-radius: 12px;
  background: rgba(52, 199, 89, 0.08);
  color: var(--c-success);
  font-size: 13px;
}

.knowledge__notice--error {
  border-color: rgba(255, 59, 48, 0.3);
  background: rgba(255, 59, 48, 0.08);
  color: var(--c-danger);
}

.knowledge__notice--warn {
  border-color: rgba(255, 149, 0, 0.3);
  background: rgba(255, 149, 0, 0.08);
  color: var(--c-warn);
}

.knowledge__notice button {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: currentColor;
  cursor: pointer;
}

.knowledge__summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}

.knowledge__summary-item {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  background: var(--bg-panel);
}

.knowledge__summary-item span {
  display: block;
  color: var(--text-sub);
  font-size: 11.5px;
}

.knowledge__summary-item strong {
  display: block;
  margin-top: 2px;
  color: var(--text-main);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}

.knowledge__tabs {
  display: flex;
  gap: 3px;
  width: fit-content;
  max-width: 100%;
  padding: 3px;
  border-radius: 12px;
  background: var(--bg-panel-2);
}

.knowledge__tabs button {
  min-height: 34px;
  padding: 6px 18px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-sub);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}

.knowledge__tabs button.knowledge__tab--active {
  background: var(--bg-panel);
  color: var(--text-main);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.knowledge__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.knowledge__actions--end {
  justify-content: flex-end;
}

.knowledge__toolbar {
  display: grid;
  grid-template-columns: minmax(200px, 1fr) 150px 130px auto;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--separator);
  background: var(--bg-panel-2);
}

.knowledge__toolbar .form-input {
  min-width: 0;
}

.knowledge__form {
  padding: 14px 16px;
  border-bottom: 1px solid var(--separator);
  background: var(--bg-panel-2);
}

.knowledge__form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.knowledge__form-grid--three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.knowledge__form-wide {
  grid-column: 1 / -1;
}

.knowledge__form .form-row {
  min-width: 0;
  margin-bottom: 12px;
}

.knowledge__textarea {
  min-height: 104px;
  resize: vertical;
}

.knowledge__textarea--code {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12.5px;
}

.knowledge__grid {
  display: grid;
  grid-template-columns: minmax(280px, 0.38fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.knowledge-table {
  min-width: 920px;
}

.knowledge-table--ontology {
  min-width: 760px;
}

.knowledge-table--relations {
  min-width: 840px;
}

.knowledge__primary {
  color: var(--text-main);
  font-weight: 500;
}

.knowledge__title-line {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  min-width: 0;
}

.knowledge__title-line .knowledge__primary {
  min-width: 0;
}

.knowledge__demo {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  min-height: 19px;
  padding: 1px 7px;
  border: 1px solid rgba(0, 122, 255, 0.18);
  border-radius: 999px;
  background: rgba(0, 122, 255, 0.09);
  color: var(--c-info);
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0;
}

.knowledge__id {
  max-width: 300px;
  margin-top: 2px;
  color: var(--text-dim);
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  font-size: 11px;
  overflow-wrap: anywhere;
  word-break: break-all;
}

.knowledge__mono {
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.knowledge__hash {
  max-width: 170px;
  overflow-wrap: anywhere;
}

.knowledge__muted {
  color: var(--text-dim);
  font-size: 12px;
}

.knowledge__pill {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 9px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-sub);
  font-size: 11px;
  white-space: nowrap;
}

.knowledge__pill--published {
  background: rgba(52, 199, 89, 0.13);
  color: var(--c-success);
}

.knowledge__pill--in_review {
  background: rgba(255, 149, 0, 0.13);
  color: var(--c-warn);
}

.knowledge__list {
  display: flex;
  flex-direction: column;
}

.knowledge__list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  padding: 12px 15px;
  border: 0;
  border-bottom: 1px solid var(--separator);
  background: transparent;
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
}

.knowledge__list-item:last-child {
  border-bottom: 0;
}

.knowledge__list-item:hover,
.knowledge__list-item--active {
  background: var(--bg-hover);
}

.knowledge__list-item > span:first-child {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.knowledge__list-item strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.knowledge__list-item small {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
  overflow-wrap: anywhere;
}

.knowledge__row-tail {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex: 0 0 auto;
  gap: 6px;
}

.knowledge__meta-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  border-bottom: 1px solid var(--separator);
}

.knowledge__meta-grid > div {
  min-width: 0;
  padding: 11px 15px;
  border-right: 1px solid var(--separator);
  border-bottom: 1px solid var(--separator);
}

.knowledge__meta-grid > div:nth-child(3n) {
  border-right: 0;
}

.knowledge__meta-grid span,
.knowledge__meta-grid strong {
  display: block;
}

.knowledge__meta-grid span {
  color: var(--text-sub);
  font-size: 11.5px;
}

.knowledge__meta-grid strong {
  margin-top: 3px;
  color: var(--text-main);
  font-size: 12.5px;
  overflow-wrap: anywhere;
}

.knowledge__meta-wide {
  grid-column: span 2;
}

.knowledge__pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--separator);
  color: var(--text-sub);
  font-size: 12px;
}

.knowledge__skeleton {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
}

.knowledge__skeleton span {
  height: 34px;
  border-radius: 8px;
  background: linear-gradient(
    90deg,
    var(--bg-panel-2) 25%,
    var(--bg-hover) 37%,
    var(--bg-panel-2) 63%
  );
  background-size: 400% 100%;
  animation: knowledge-shimmer 1.4s ease infinite;
}

@keyframes knowledge-shimmer {
  0% { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}

.knowledge__error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 12px 16px;
  padding: 10px 12px;
  border: 1px solid rgba(255, 59, 48, 0.3);
  border-radius: 12px;
  background: rgba(255, 59, 48, 0.07);
  color: var(--c-danger);
  font-size: 12.5px;
}

.knowledge__review-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--separator);
  background: var(--bg-panel-2);
}

.knowledge__review-bar .form-input {
  flex: 1;
  min-width: 0;
}

.knowledge__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 16px 8px;
  color: var(--text-main);
  font-size: 13px;
}

.knowledge__section-head span {
  color: var(--text-dim);
}

.knowledge__row-actions {
  display: flex;
  gap: 10px;
}

.knowledge__link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--c-info);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.knowledge__link--danger {
  color: var(--c-danger);
}

.knowledge__results {
  display: flex;
  flex-direction: column;
}

.knowledge__examples {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  padding: 11px 16px;
  border-bottom: 1px solid var(--separator);
  background: var(--bg-panel);
}

.knowledge__examples > span {
  margin-right: 2px;
  color: var(--text-dim);
  font-size: 11.5px;
}

.knowledge__examples button {
  min-height: 28px;
  padding: 4px 10px;
  border: 1px solid var(--panel-border);
  border-radius: 999px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font: inherit;
  font-size: 11.5px;
  line-height: 1.35;
  cursor: pointer;
}

.knowledge__examples button:hover:not(:disabled) {
  border-color: rgba(0, 122, 255, 0.3);
  color: var(--c-info);
}

.knowledge__examples button:disabled {
  cursor: default;
  opacity: 0.55;
}

.knowledge__result {
  min-width: 0;
  padding: 14px 16px;
  border-bottom: 1px solid var(--separator);
}

.knowledge__result:last-child {
  border-bottom: 0;
}

.knowledge__result-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.knowledge__result-head strong {
  color: var(--text-main);
  font-size: 14px;
}

.knowledge__result-head span {
  flex-shrink: 0;
  color: var(--text-sub);
  font-size: 11.5px;
}

.knowledge__result p {
  margin: 8px 0 0;
  color: var(--text-sub);
  font-size: 12.5px;
  line-height: 1.7;
}

.knowledge__path {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 9px;
}

.knowledge__path span {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--bg-panel-2);
  color: var(--c-info);
  font-size: 10.5px;
}

.knowledge__citations {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 10px;
  padding-left: 10px;
  border-left: 2px solid var(--panel-border);
  color: var(--text-sub);
  font-size: 11.5px;
  line-height: 1.6;
}

.knowledge__evidence {
  display: flex;
  flex-direction: column;
}

.knowledge__evidence-item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 10px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--separator);
}

.knowledge__evidence-item:last-child {
  border-bottom: 0;
}

.knowledge__evidence-rank {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--bg-active);
  color: var(--c-primary);
  font-size: 12px;
  font-weight: 600;
}

.knowledge__evidence-item p {
  margin: 0;
  color: var(--text-main);
  font-size: 12.5px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.knowledge__result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 12px;
  margin-top: 6px;
  color: var(--text-dim);
  font-size: 10.5px;
}

.knowledge-vision__engine {
  color: var(--text-dim);
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  font-size: 11.5px;
  font-weight: 500;
}

.knowledge-vision__intro {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--separator);
  background: var(--bg-panel-2);
}

.knowledge-vision__intro strong {
  display: block;
  color: var(--text-main);
  font-size: 13px;
}

.knowledge-vision__intro p {
  margin: 3px 0 0;
  color: var(--text-sub);
  font-size: 12px;
}

.knowledge-vision__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  background: var(--separator);
}

.knowledge-vision__sample {
  min-width: 0;
  padding: 16px;
  background: var(--bg-panel);
}

.knowledge-vision__preview {
  position: relative;
  display: block;
  width: 100%;
  min-height: 180px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--panel-border);
  border-radius: 9px;
  background: #111;
  cursor: pointer;
}

.knowledge-vision__preview img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.knowledge-vision__box {
  position: absolute;
  z-index: 1;
  min-width: 8px;
  min-height: 8px;
  border: 2px solid;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.25);
  pointer-events: none;
}

.knowledge-vision__box-label {
  position: absolute;
  top: 0;
  left: 0;
  max-width: 150px;
  padding: 1px 4px;
  overflow: hidden;
  color: #fff;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-vision__count {
  position: absolute;
  right: 8px;
  bottom: 8px;
  z-index: 2;
  padding: 3px 8px;
  border: 1px solid rgba(255, 255, 255, 0.22);
  border-radius: 999px;
  background: rgba(18, 18, 20, 0.72);
  color: #fff;
  font-size: 10.5px;
  font-weight: 600;
  backdrop-filter: blur(10px);
}

.knowledge-vision__body {
  padding-top: 13px;
}

.knowledge-vision__title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.knowledge-vision__title-row strong {
  min-width: 0;
  color: var(--text-main);
  font-size: 14px;
  line-height: 1.45;
}

.knowledge-vision__body > p {
  margin: 7px 0 0;
  color: var(--text-sub);
  font-size: 12px;
  line-height: 1.65;
}

.knowledge-vision__metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 12px;
  margin-top: 9px;
  color: var(--text-dim);
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  font-size: 10.5px;
}

.knowledge-vision__body .knowledge-vision__review {
  color: var(--text-main);
}

.knowledge-vision__source {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 4px 12px;
  margin-top: 10px;
  padding-top: 9px;
  border-top: 1px solid var(--separator);
  color: var(--text-dim);
  font-size: 10.5px;
}

.knowledge-vision__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 11px;
}

.knowledge-vision__actions a,
.knowledge-vision__actions button {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--c-info);
  font-size: 11.5px;
  cursor: pointer;
}

.knowledge-vision__actions button {
  font-weight: 600;
}

.knowledge-vision__disclaimer {
  padding: 12px 16px;
  border-top: 1px solid var(--separator);
  background: var(--bg-panel-2);
  color: var(--text-dim);
  font-size: 11px;
  line-height: 1.65;
}

@media (max-width: 1100px) {
  .knowledge__summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .knowledge__grid {
    grid-template-columns: 1fr;
  }

  .knowledge__form-grid--three {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .knowledge-vision__grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .knowledge__head {
    align-items: center;
  }

  .knowledge__title {
    font-size: 18px;
  }

  .knowledge__summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .knowledge__toolbar {
    grid-template-columns: 1fr 1fr;
  }

  .knowledge__toolbar .form-input:first-child {
    grid-column: 1 / -1;
  }

  .knowledge__toolbar .btn {
    width: 100%;
  }

  .knowledge__form-grid,
  .knowledge__form-grid--three {
    grid-template-columns: 1fr;
  }

  .knowledge__meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .knowledge__meta-grid > div:nth-child(3n) {
    border-right: 1px solid var(--separator);
  }

  .knowledge__meta-grid > div:nth-child(2n) {
    border-right: 0;
  }

  .knowledge__meta-wide {
    grid-column: 1 / -1;
  }

  .knowledge__review-bar {
    align-items: stretch;
    flex-direction: column;
  }

  .knowledge__review-bar .btn {
    width: 100%;
  }

  .knowledge__pager {
    justify-content: space-between;
  }

  .knowledge-vision__intro {
    align-items: flex-start;
    flex-direction: column;
  }
}

@media (max-width: 480px) {
  .knowledge .panel-title {
    flex-wrap: wrap;
  }

  .knowledge .panel-title > span:first-child {
    flex: 1 0 100%;
    white-space: nowrap;
  }

  .knowledge .panel-title > .knowledge__actions {
    width: 100%;
  }

  .knowledge .panel-title > .knowledge__actions .btn {
    flex: 1;
  }

  .knowledge__head {
    align-items: stretch;
    flex-direction: column;
  }

  .knowledge__head > .btn {
    width: 100%;
  }

  .knowledge__tabs {
    width: 100%;
  }

  .knowledge__tabs button {
    flex: 1;
    padding-inline: 8px;
  }

  .knowledge__summary {
    gap: 6px;
  }

  .knowledge__summary-item {
    padding: 9px 10px;
  }

  .knowledge__list-item {
    align-items: flex-start;
    flex-direction: column;
  }

  .knowledge__row-tail {
    width: 100%;
    justify-content: flex-start;
  }

  .knowledge__toolbar {
    grid-template-columns: 1fr;
  }

  .knowledge__toolbar .form-input:first-child {
    grid-column: auto;
  }

  .knowledge__form,
  .knowledge__toolbar,
  .knowledge__review-bar {
    padding-inline: 12px;
  }

  .knowledge-vision__sample {
    padding: 13px 12px;
  }

  .knowledge-vision__title-row,
  .knowledge-vision__actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .knowledge-vision__box-label {
    max-width: 105px;
    font-size: 8px;
  }

  .knowledge__meta-grid {
    grid-template-columns: 1fr;
  }

  .knowledge__meta-grid > div,
  .knowledge__meta-grid > div:nth-child(2n),
  .knowledge__meta-grid > div:nth-child(3n) {
    border-right: 0;
  }

  .knowledge__meta-wide {
    grid-column: auto;
  }

  .knowledge__result-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .knowledge__actions,
  .knowledge__actions--end {
    width: 100%;
  }

  .knowledge__actions .btn,
  .knowledge__actions--end .btn {
    flex: 1;
  }

  .knowledge__examples {
    align-items: stretch;
    flex-direction: column;
    padding-inline: 12px;
  }

  .knowledge__examples button {
    width: 100%;
    border-radius: 10px;
    text-align: left;
  }

  .knowledge__pager {
    flex-wrap: wrap;
  }

  .knowledge__pager span {
    order: -1;
    width: 100%;
    text-align: center;
  }
}
</style>
