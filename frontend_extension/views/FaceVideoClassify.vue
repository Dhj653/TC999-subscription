<!--
  人脸视频分类 - 独立页面
  说明：
  1. 完全独立，不修改萤核原有任何页面/逻辑。
  2. 通过 HTTP 调用外挂 Python 服务（默认 127.0.0.1:5002）。
  3. 仅依赖 Vue3，不引入额外 UI 框架，样式自包含。
-->
<template>
  <div class="fv-page">
    <header class="fv-header">
      <h2>人脸视频分类</h2>
      <span class="fv-sub">外挂服务端口：{{ base.replace('http://', '') }}</span>
      <span class="fv-badge" :class="serviceOk ? 'ok' : 'err'">
        {{ serviceOk ? '服务在线' : '服务未连接' }}
      </span>
    </header>

    <!-- 配置区 -->
    <section class="fv-card">
      <div class="fv-row">
        <label class="fv-label">源文件夹</label>
        <input v-model="scanFolder" class="fv-input flex1" placeholder="请输入要扫描的源目录绝对路径" />
      </div>
      <div class="fv-row">
        <label class="fv-checkbox">
          <input type="checkbox" v-model="customOutput" /> 自定义输出文件夹
        </label>
        <input v-if="customOutput" v-model="outputDir" class="fv-input flex1"
               placeholder="自定义输出目录绝对路径（不勾选则在源目录内分类）" />
      </div>
      <div class="fv-row">
        <label class="fv-checkbox">
          <input type="checkbox" v-model="testMode" /> 测试预览模式
          <small>（开启：只生成虚拟结果，不移动磁盘文件）</small>
        </label>
        <div class="fv-slider">
          <label>人脸相似度阈值：{{ similarity.toFixed(2) }}</label>
          <input type="range" min="0.3" max="0.95" step="0.01" v-model.number="similarity" />
          <small>越大越严格</small>
        </div>
      </div>
      <div class="fv-row fv-actions">
        <button class="fv-btn primary" :disabled="running || !scanFolder" @click="startScan">启动任务</button>
        <button class="fv-btn warn" :disabled="!running" @click="stopTask">停止任务</button>
        <button class="fv-btn" @click="loadConfig">读取配置</button>
      </div>
    </section>

    <!-- 进度 + 日志 -->
    <section class="fv-card">
      <div class="fv-row between">
        <strong>任务进度</strong>
        <span v-if="latest">
          {{ latest.status }} | {{ latest.processed_videos }}/{{ latest.total_videos }}
          <span v-if="latest.current_video"> | 当前：{{ latest.current_video }}</span>
        </span>
      </div>
      <div class="fv-progress">
        <div class="fv-progress-bar" :style="{ width: (latest?.progress || 0) + '%' }"></div>
      </div>
      <div class="fv-log" ref="logBox">{{ logs || '（实时日志将显示在此）' }}</div>
    </section>

    <!-- 全局操作 -->
    <section class="fv-card">
      <div class="fv-row between">
        <strong>全局操作</strong>
        <div>
          <button class="fv-btn" @click="loadGroups">刷新分组</button>
          <button class="fv-btn" @click="previewMove">预览移动结果</button>
          <button class="fv-btn danger" :disabled="!testMode" @click="confirmExecuteMove">执行移动文件</button>
          <button class="fv-btn danger" @click="revertAll">一键回滚全部</button>
        </div>
      </div>
      <small v-if="mergeSourceId" class="fv-tip">
        已选择合并源：分组 #{{ mergeSourceId }}。点击目标分组的「合并到此」按钮完成合并，或
        <a href="javascript:void(0)" @click="mergeSourceId = null">取消</a>。
      </small>
    </section>

    <!-- 分组列表 -->
    <section class="fv-card">
      <strong>人物分组（{{ groups.length }}）</strong>
      <div v-for="g in groups" :key="g.group_id" class="fv-group">
        <div class="fv-group-head">
          <span class="fv-group-name">{{ g.group_name }}</span>
          <span class="fv-tag" :class="tagClass(g.status)">{{ g.status_label }}</span>
          <span class="fv-count">视频 {{ g.video_count }}</span>
          <div class="fv-spacer"></div>
          <button class="fv-btn sm" @click="viewNames(g)">人名</button>
          <button class="fv-btn sm" @click="setMergeSource(g)">设为合并源</button>
          <button class="fv-btn sm" v-if="mergeSourceId && mergeSourceId !== g.group_id" @click="mergeTo(g)">合并到此</button>
          <button class="fv-btn sm" @click="renameGroup(g)">改名</button>
          <button class="fv-btn sm" @click="reprocessGroup(g)">重处理</button>
          <button class="fv-btn sm danger" @click="deleteGroup(g)">删除</button>
          <button class="fv-btn sm" @click="toggleExpand(g)">{{ g._expand ? '收起' : '展开' }}</button>
        </div>
        <div v-if="g._expand" class="fv-videos">
          <div v-for="v in g.videos" :key="v.mapping_id" class="fv-video">
            <span :title="v.video_path">{{ shortName(v.video_path) }}</span>
            <span class="fv-mini-tag">{{ v.source === 'archive' ? '压缩包' : '文件' }}</span>
            <span class="fv-mini-tag" :class="v.moved ? 'moved' : ''">{{ v.moved ? '已移动' : '未移动' }}</span>
            <button class="fv-btn sm" @click="reprocessOne(v)">重处理</button>
          </div>
          <div v-if="!g.videos?.length" class="fv-empty">无关联视频</div>
        </div>
      </div>
      <div v-if="!groups.length" class="fv-empty">暂无分组，请先启动任务。</div>
    </section>

    <!-- 人名弹窗 -->
    <div v-if="namesModal.show" class="fv-modal" @click.self="namesModal.show = false">
      <div class="fv-modal-box">
        <h3>分组 #{{ namesModal.groupId }} 提取的人名</h3>
        <div v-if="namesModal.names.length">
          <span v-for="n in namesModal.names" :key="n" class="fv-chip">{{ n }}</span>
        </div>
        <div v-else class="fv-empty">未解析出任何人名</div>
        <p v-if="namesModal.conflict" class="fv-warn">⚠ 检测到多名称冲突，已交人工确认。</p>
        <button class="fv-btn" @click="namesModal.show = false">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'

// 外挂服务地址
const base = 'http://127.0.0.1:5002'

const scanFolder = ref('')
const customOutput = ref(false)
const outputDir = ref('')
const testMode = ref(true)
const similarity = ref(0.55)

const running = ref(false)
const latest = ref(null)
const logs = ref('')
const groups = ref([])
const serviceOk = ref(false)
const mergeSourceId = ref(null)
const logBox = ref(null)

let pollTimer = null

const api = async (path, opts = {}) => {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch (e) { /* noop */ }
    throw new Error(detail)
  }
  return res.json()
}

const checkService = async () => {
  try {
    await api('/api/health')
    serviceOk.value = true
  } catch (e) {
    serviceOk.value = false
  }
}

const loadConfig = async () => {
  try {
    const r = await api('/api/config')
    similarity.value = r.data.similarity
    testMode.value = r.data.test_preview_mode
  } catch (e) { /* 忽略，使用默认值 */ }
}

const startScan = async () => {
  if (!scanFolder.value) return
  try {
    const body = {
      scan_folder: scanFolder.value,
      output_dir: customOutput.value ? outputDir.value : null,
      test_mode: testMode.value,
      similarity: similarity.value,
    }
    const r = await api('/api/scan_folder', {
      method: 'POST', body: JSON.stringify(body),
    })
    running.value = true
    startPolling()
    alert(r.message)
  } catch (e) {
    alert('启动失败：' + e.message)
  }
}

const stopTask = async () => {
  try {
    await api('/api/stop_task', { method: 'POST' })
  } catch (e) { /* noop */ }
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    try {
      const r = await api('/api/task_status')
      const st = r.data
      serviceOk.value = true
      latest.value = st.latest
      running.value = st.running_task_id != null
      if (st.latest) {
        logs.value = st.latest.logs || ''
        await nextTick()
        if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
      }
      // 任务完成时刷新分组
      if (st.latest && ['completed', 'failed', 'cancelled'].includes(st.latest.status)) {
        loadGroups()
      }
    } catch (e) {
      serviceOk.value = false
    }
  }, 1500)
}

const loadGroups = async () => {
  try {
    const r = await api('/api/person_groups')
    const list = r.data.groups || []
    list.forEach(g => { g._expand = g._expand ?? false })
    groups.value = list
  } catch (e) {
    alert('加载分组失败：' + e.message)
  }
}

const previewMove = async () => {
  try {
    const r = await api('/api/move_files', {
      method: 'POST', body: JSON.stringify({ test_mode: true }),
    })
    alert(r.message + `\n共 ${r.data.results?.length || 0} 项`)
  } catch (e) { alert('预览失败：' + e.message) }
}

const confirmExecuteMove = async () => {
  if (!confirm('确认执行真实文件移动？此操作将移动磁盘文件。')) return
  try {
    const r = await api('/api/move_files', {
      method: 'POST', body: JSON.stringify({ test_mode: false }),
    })
    alert(r.message)
    loadGroups()
  } catch (e) { alert('移动失败：' + e.message) }
}

const revertAll = async () => {
  if (!latest.value) return alert('未找到当前任务')
  if (!confirm('确认一键回滚所有已移动文件并恢复分组原名？')) return
  try {
    const r = await api('/api/revert_all_files?task_id=' + latest.value.id, { method: 'POST' })
    alert(r.message)
    loadGroups()
  } catch (e) { alert('回滚失败：' + e.message) }
}

const namesModal = ref({ show: false, groupId: null, names: [], conflict: false })

const viewNames = async (g) => {
  try {
    const r = await api('/api/group_extract_names?group_id=' + g.group_id)
    namesModal.value = {
      show: true, groupId: g.group_id,
      names: r.data.extracted_names || [],
      conflict: r.data.has_conflict,
    }
  } catch (e) { alert('查询失败：' + e.message) }
}

const setMergeSource = (g) => { mergeSourceId.value = g.group_id }

const mergeTo = async (g) => {
  if (!mergeSourceId.value) return
  if (!confirm(`确认将分组 #${mergeSourceId.value} 合并到「${g.group_name}」？`)) return
  try {
    const r = await api('/api/merge_group', {
      method: 'POST',
      body: JSON.stringify({ source_group_id: mergeSourceId.value, target_group_id: g.group_id }),
    })
    alert(r.message)
    mergeSourceId.value = null
    loadGroups()
  } catch (e) { alert('合并失败：' + e.message) }
}

const renameGroup = async (g) => {
  const name = prompt('请输入新的分组名称：', g.group_name)
  if (!name) return
  try {
    await api('/api/group_rename?group_id=' + g.group_id + '&new_name=' + encodeURIComponent(name), {
      method: 'PUT',
    })
    // 也可用 body：fetch 不便带 PUT query，上面已用 query 形式
    loadGroups()
  } catch (e) { alert('改名失败：' + e.message) }
}

const reprocessOne = async (v) => {
  if (!confirm('确认重新处理该视频？')) return
  try {
    const r = await api('/api/reprocess_single', {
      method: 'POST', body: JSON.stringify({ mapping_id: v.mapping_id }),
    })
    alert(r.message)
    loadGroups()
  } catch (e) { alert('重处理失败：' + e.message) }
}

const reprocessGroup = async (g) => {
  if (!g.videos?.length) return alert('该分组无视频')
  if (!confirm(`确认重新处理分组「${g.group_name}」下全部 ${g.videos.length} 个视频？`)) return
  for (const v of g.videos) {
    try {
      await api('/api/reprocess_single', {
        method: 'POST', body: JSON.stringify({ mapping_id: v.mapping_id }),
      })
    } catch (e) { /* 继续 */ }
  }
  alert('已批量重新处理')
  loadGroups()
}

const deleteGroup = async (g) => {
  if (!confirm(`确认删除分组「${g.group_name}」？（仅删分组记录，不删磁盘文件）`)) return
  try {
    const r = await api('/api/delete_group?group_id=' + g.group_id, { method: 'POST' })
    alert(r.message)
    loadGroups()
  } catch (e) { alert('删除失败：' + e.message) }
}

const toggleExpand = (g) => { g._expand = !g._expand }

const tagClass = (s) => ({
  auto_numbered: 't-blue',
  renamed: 't-green',
  name_conflict: 't-red',
  multi_person: 't-orange',
}[s] || 't-gray')

const shortName = (p) => {
  if (!p) return ''
  const parts = p.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1]
}

onMounted(async () => {
  await checkService()
  await loadConfig()
  await loadGroups()
  startPolling()
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.fv-page { padding: 16px; max-width: 1100px; margin: 0 auto; color: #e6e6e6; font-size: 14px; }
.fv-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.fv-header h2 { margin: 0; }
.fv-sub { color: #999; font-size: 12px; }
.fv-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.fv-badge.ok { background: #1f7a3d; color: #fff; }
.fv-badge.err { background: #8a2b2b; color: #fff; }
.fv-card { background: #1e1e22; border: 1px solid #333; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
.fv-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
.fv-row:last-child { margin-bottom: 0; }
.fv-row.between { justify-content: space-between; }
.fv-label { width: 90px; color: #aaa; }
.fv-input { background: #2a2a2f; border: 1px solid #444; color: #eee; border-radius: 6px; padding: 7px 10px; }
.fv-input.flex1 { flex: 1; min-width: 220px; }
.fv-checkbox { display: flex; align-items: center; gap: 6px; }
.fv-checkbox small { color: #888; }
.fv-slider { display: flex; align-items: center; gap: 8px; }
.fv-slider input[type=range] { width: 180px; }
.fv-actions { gap: 8px; }
.fv-btn { background: #333; color: #eee; border: 1px solid #444; border-radius: 6px; padding: 7px 14px; cursor: pointer; }
.fv-btn:hover { background: #3d3d44; }
.fv-btn:disabled { opacity: .45; cursor: not-allowed; }
.fv-btn.primary { background: #2563eb; border-color: #2563eb; }
.fv-btn.warn { background: #b45309; border-color: #b45309; }
.fv-btn.danger { background: #b91c1c; border-color: #b91c1c; }
.fv-btn.sm { padding: 3px 8px; font-size: 12px; }
.fv-progress { height: 14px; background: #2a2a2f; border-radius: 7px; overflow: hidden; margin: 6px 0; }
.fv-progress-bar { height: 100%; background: linear-gradient(90deg,#2563eb,#22c55e); transition: width .3s; }
.fv-log { background: #141416; border: 1px solid #333; border-radius: 6px; padding: 8px; height: 180px; overflow: auto; font-family: Consolas, monospace; font-size: 12px; white-space: pre-wrap; color: #b9f6ca; }
.fv-group { border: 1px solid #333; border-radius: 6px; padding: 8px 10px; margin-top: 8px; }
.fv-group-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fv-group-name { font-weight: 600; }
.fv-spacer { flex: 1; }
.fv-tag { padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.fv-count { color: #999; font-size: 12px; }
.t-blue { background: #1e3a8a; } .t-green { background: #166534; } .t-red { background: #7f1d1d; } .t-orange { background: #9a3412; } .t-gray { background: #444; }
.fv-videos { margin-top: 8px; border-top: 1px dashed #333; padding-top: 6px; }
.fv-video { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 12px; }
.fv-video span { color: #ccc; }
.fv-mini-tag { padding: 1px 6px; border-radius: 8px; background: #333; font-size: 10px; color: #ccc; }
.fv-mini-tag.moved { background: #166534; color: #fff; }
.fv-empty { color: #777; padding: 8px 0; }
.fv-tip { color: #fbbf24; margin-top: 6px; display: block; }
.fv-tip a { color: #60a5fa; }
.fv-modal { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex; align-items: center; justify-content: center; z-index: 999; }
.fv-modal-box { background: #1e1e22; border: 1px solid #444; border-radius: 8px; padding: 18px; min-width: 320px; max-width: 480px; }
.fv-chip { display: inline-block; background: #2a2a2f; border: 1px solid #444; border-radius: 12px; padding: 3px 10px; margin: 3px; }
.fv-warn { color: #fbbf24; }
</style>
