<!--
  【新增】角色管理页面 — 独立侧边栏菜单
  需求：
  - 展示所有角色（缩略图 / 名称 / 视频数 / 文件夹路径）
  - 重命名角色 → 后端联动重命名磁盘文件夹
  - 删除角色 → 仅软删数据库，不删文件夹/视频，提示用户自行打开文件夹决定
  - "打开定位文件夹" → 调后端拿到 folder_path，前端通过 IPC 打开系统文件管理器
  - "工作文件夹配置" 区块：保存工作文件夹路径 + 是否启动时自动扫描
-->
<template>
  <div class="cm-page">
    <header class="cm-header">
      <h2>角色管理</h2>
      <span class="cm-sub">外挂服务：{{ base.replace('http://', '') }}</span>
      <span class="cm-badge" :class="serviceOk ? 'ok' : 'err'">
        {{ serviceOk ? '服务在线' : '服务未连接' }}
      </span>
      <div style="flex:1"></div>
      <button class="cm-btn primary" :disabled="loading" @click="refresh">刷新</button>
    </header>

    <!-- 工作文件夹设置 -->
    <section class="cm-card">
      <h3>工作文件夹配置</h3>
      <div class="cm-row">
        <label class="cm-label">工作文件夹</label>
        <input v-model="workFolder" class="cm-input flex1"
               placeholder="用户把新视频放此目录，服务自动识别分类移动到对应子文件夹（绝对路径）" />
      </div>
      <div class="cm-row">
        <label class="cm-checkbox">
          <input type="checkbox" v-model="autoScanOnStart" />
          记住该路径，下次可一键扫描
        </label>
        <div style="flex:1"></div>
        <button class="cm-btn" @click="saveWorkFolder">保存配置</button>
        <button class="cm-btn primary" :disabled="!workFolder || running" @click="quickScanWorkFolder">
          扫描此工作文件夹（{{folderCreateMinVideos}}+视频同人才建夹）
        </button>
      </div>
      <small class="cm-hint">
        说明：检测到 <b>≥ {{ folderCreateMinVideos }} 个相同女性</b> 的视频才会创建角色文件夹并移动；
        不足 {{ folderCreateMinVideos }} 个的保留在原位（或归"未分类"）。戴口罩的女性也可识别分类。
      </small>
    </section>

    <!-- 任务进度（与扫描页面联动） -->
    <section v-if="latest" class="cm-card">
      <div class="cm-row between">
        <strong>当前任务进度</strong>
        <span>{{ latest.status }} | {{ latest.processed_videos }}/{{ latest.total_videos }}</span>
      </div>
      <div class="cm-progress">
        <div class="cm-progress-bar" :style="{ width: (latest.progress || 0) + '%' }"></div>
      </div>
    </section>

    <!-- 角色列表 -->
    <section class="cm-card">
      <div class="cm-row between">
        <h3>角色列表（{{ characters.length }}）</h3>
        <label class="cm-checkbox">
          <input type="checkbox" v-model="includeDeleted" @change="refresh" /> 显示已删除
        </label>
      </div>

      <div v-if="loading" class="cm-empty">加载中...</div>
      <div v-else-if="!characters.length" class="cm-empty">
        暂无角色。请先去「人脸视频分类」扫描 ≥ {{ folderCreateMinVideos }} 个相同女性的视频，
        或在上面点「扫描此工作文件夹」。
      </div>

      <div class="cm-grid">
        <div v-for="c in characters" :key="c.character_id" class="cm-card-slot"
             :class="{ deleted: c.status === 'deleted' }">
          <div class="cm-thumb">
            <img v-if="c.thumbnail_path"
                 :src="base + '/api/characters/thumbnail/' + c.character_id"
                 :alt="c.name" loading="lazy"
                 @error="onThumbErr(c.character_id)" />
            <div v-else class="cm-thumb-ph">
              {{ c.name.slice(0, 1) }}
            </div>
            <span v-if="c.status === 'deleted'" class="cm-deleted-tag">已删除</span>
          </div>
          <div class="cm-info">
            <div class="cm-name" :title="c.name">{{ c.name }}</div>
            <div class="cm-meta">
              <span>📹 {{ c.video_count }} 个视频</span>
              <span>·</span>
              <span>ID #{{ c.character_id }}</span>
            </div>
            <div class="cm-folder" :title="c.folder_path">
              {{ c.folder_path ? shortFolder(c.folder_path) : '（尚未创建文件夹）' }}
            </div>
            <div class="cm-actions">
              <button class="cm-btn sm" @click="renameCharacter(c)"
                      :disabled="c.status === 'deleted'">重命名</button>
              <button class="cm-btn sm" @click="openFolder(c)"
                      :disabled="!c.folder_path">打开文件夹</button>
              <button class="cm-btn sm danger" @click="deleteCharacter(c)"
                      :disabled="c.status === 'deleted'">删除角色</button>
            </div>
          </div>
        </div>
      </div>

      <small class="cm-hint">
        「删除角色」仅删除数据库记录，<b>不会删除文件夹和视频</b>。请点击「打开文件夹」后自行决定是否物理删除。
      </small>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const base = 'http://127.0.0.1:5002'

const serviceOk = ref(false)
const loading = ref(false)
const characters = ref([])
const includeDeleted = ref(false)

const workFolder = ref('')
const autoScanOnStart = ref(false)
const folderCreateMinVideos = ref(2)
const running = ref(false)
const latest = ref(null)

let pollTimer = null

const api = async (path, opts = {}) => {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch (e) { /* noop */ }
    throw new Error(detail)
  }
  return res.json()
}

const checkService = async () => {
  try { await api('/api/health'); serviceOk.value = true }
  catch (e) { serviceOk.value = false }
}

const refresh = async () => {
  loading.value = true
  try {
    await checkService()
    const r = await api('/api/characters?include_deleted=' + (includeDeleted.value ? '1' : '0'))
    characters.value = r.data.characters || []
    // 读取配置
    const cfg = await api('/api/config')
    folderCreateMinVideos.value = cfg.data.folder_create_min_videos || 2
    const wf = await api('/api/settings/work_folder')
    workFolder.value = wf.data.work_folder || ''
    autoScanOnStart.value = wf.data.auto_scan_on_start
  } catch (e) {
    alert('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}

const saveWorkFolder = async () => {
  if (!workFolder.value) return alert('请先填写工作文件夹路径')
  try {
    const r = await api(
      '/api/settings/work_folder?work_folder=' + encodeURIComponent(workFolder.value)
      + '&auto_scan_on_start=' + (autoScanOnStart.value ? 'true' : 'false'),
      { method: 'POST' }
    )
    alert(r.message)
  } catch (e) { alert('保存失败：' + e.message) }
}

const quickScanWorkFolder = async () => {
  if (!workFolder.value) return alert('请先填写并保存工作文件夹路径')
  try {
    const r = await api('/api/scan_folder', {
      method: 'POST',
      body: JSON.stringify({
        scan_folder: workFolder.value,
        output_dir: workFolder.value,
        test_mode: false,
        similarity: 0.55,
        use_character_library: true,
      }),
    })
    alert(r.message)
    running.value = true
    startPolling()
  } catch (e) { alert('启动扫描失败：' + e.message) }
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    try {
      const r = await api('/api/task_status')
      const st = r.data
      latest.value = st.latest
      running.value = st.running_task_id != null
      if (st.latest && ['completed', 'failed', 'cancelled'].includes(st.latest.status)) {
        clearInterval(pollTimer); pollTimer = null
        running.value = false
        refresh()
      }
    } catch (e) { /* noop */ }
  }, 1500)
}

const renameCharacter = async (c) => {
  const name = prompt('请输入新的角色名称（同时会联动重命名磁盘文件夹）：', c.name)
  if (!name || name === c.name) return
  try {
    const r = await api(
      '/api/characters/rename?character_id=' + c.character_id
      + '&new_name=' + encodeURIComponent(name),
      { method: 'PUT' }
    )
    alert(r.message)
    refresh()
  } catch (e) { alert('重命名失败：' + e.message) }
}

const deleteCharacter = async (c) => {
  if (!confirm(
    `确认删除角色「${c.name}」？\n\n`
    + '⚠ 仅删除数据库记录，不会删除磁盘文件夹和视频。\n'
    + '  你可以随后点「打开文件夹」定位后自行删除磁盘文件。'
  )) return
  try {
    const r = await api('/api/characters/delete?character_id=' + c.character_id,
                        { method: 'POST' })
    alert(r.message + (r.data?.folder_path ? `\n文件夹：${r.data.folder_path}` : ''))
    refresh()
  } catch (e) { alert('删除失败：' + e.message) }
}

const openFolder = async (c) => {
  try {
    const r = await api('/api/characters/open_folder?character_id=' + c.character_id,
                        { method: 'POST' })
    if (!r.success) return alert(r.message)
    const d = r.data
    // 优先通过萤核 Electron IPC 打开文件管理器
    if (window.electronAPI?.dataDirectory?.showItemInFolder) {
      try {
        await window.electronAPI.dataDirectory.showItemInFolder(d.folder_path)
        return
      } catch (e) { /* 回退到命令提示 */ }
    }
    if (window.require?.('electron')?.shell?.showItemInFolder) {
      try {
        window.require('electron').shell.showItemInFolder(d.folder_path)
        return
      } catch (e) { /* noop */ }
    }
    // 非Electron环境：给出命令提示 + 复制路径
    const copy = `文件夹路径：\n${d.folder_path}\n\n命令提示：\n${d.command_hint}`
    if (navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(d.folder_path)
        alert('已复制文件夹路径到剪贴板：\n' + d.folder_path + '\n\n' + d.command_hint)
        return
      } catch (e) { /* noop */ }
    }
    alert(copy)
  } catch (e) { alert('获取文件夹信息失败：' + e.message) }
}

const onThumbErr = (cid) => { /* 加载失败则降级为首字母占位，已由初始 v-if 处理 */ }

const shortFolder = (p) => {
  if (!p) return ''
  const parts = p.replace(/\\/g, '/').split('/')
  return parts.length <= 3 ? p : '.../' + parts.slice(-2).join('/')
}

onMounted(() => { refresh() })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.cm-page { padding: 16px; max-width: 1200px; margin: 0 auto; color: #e6e6e6; font-size: 14px; }
.cm-header { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
.cm-header h2 { margin:0; }
.cm-sub { color:#999; font-size:12px; }
.cm-badge { padding:2px 10px; border-radius:10px; font-size:12px; }
.cm-badge.ok { background:#1f7a3d; } .cm-badge.err { background:#8a2b2b; }
.cm-card { background:#1e1e22; border:1px solid #333; border-radius:8px; padding:14px; margin-bottom:14px; }
.cm-card h3 { margin:0 0 10px; font-size:15px; }
.cm-row { display:flex; align-items:center; gap:12px; margin-bottom:10px; flex-wrap:wrap; }
.cm-row:last-child { margin-bottom:0; } .cm-row.between { justify-content:space-between; }
.cm-label { width: 90px; color: #aaa; }
.cm-input { background:#2a2a2f; border:1px solid #444; color:#eee; border-radius:6px; padding:7px 10px; }
.cm-input.flex1 { flex:1; min-width:240px; }
.cm-checkbox { display:flex; align-items:center; gap:6px; }
.cm-hint { display:block; color:#888; margin-top:6px; }
.cm-btn { background:#333; color:#eee; border:1px solid #444; border-radius:6px; padding:7px 14px; cursor:pointer; }
.cm-btn:hover:not(:disabled) { background:#3d3d44; } .cm-btn:disabled { opacity:.4; cursor:not-allowed; }
.cm-btn.primary { background:#2563eb; border-color:#2563eb; }
.cm-btn.danger { background:#b91c1c; border-color:#b91c1c; } .cm-btn.sm { padding:3px 8px; font-size:12px; }
.cm-progress { height:12px; background:#2a2a2f; border-radius:6px; overflow:hidden; margin-top:6px; }
.cm-progress-bar { height:100%; background:linear-gradient(90deg,#2563eb,#22c55e); transition: width .3s; }
.cm-empty { color:#777; padding:18px 0; text-align:center; }

.cm-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.cm-card-slot { background:#26262b; border:1px solid #333; border-radius:8px; padding:10px; display:flex; gap:12px; align-items:flex-start; }
.cm-card-slot.deleted { opacity:.45; }
.cm-thumb { width:86px; height:86px; background:#1a1a1e; border-radius:6px; overflow:hidden; flex-shrink:0; position:relative; display:flex; align-items:center; justify-content:center; }
.cm-thumb img { width:100%; height:100%; object-fit:cover; }
.cm-thumb-ph { font-size:30px; color:#555; font-weight:bold; }
.cm-deleted-tag { position:absolute; inset:auto 0 0 0; background:#8a2b2b; text-align:center; font-size:10px; padding:2px; }
.cm-info { flex:1; min-width:0; }
.cm-name { font-weight:600; margin-bottom:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cm-meta { font-size:11px; color:#999; display:flex; gap:6px; flex-wrap:wrap; margin-bottom:4px; }
.cm-folder { font-size:11px; color:#6b8ca5; margin-bottom:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cm-actions { display:flex; gap:6px; flex-wrap:wrap; }
</style>
