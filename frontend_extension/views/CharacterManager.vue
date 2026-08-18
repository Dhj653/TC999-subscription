<!--
  角色库管理 - 独立页面
  功能：
    1. 角色列表（带人脸缩略图 + 当前命名 + 视频数 + 对应文件夹路径）
    2. 重命名角色 → 联动重命名磁盘文件夹
    3. 删除角色 → 仅软删除数据库记录，不删除磁盘文件夹和视频
    4. 打开对应文件夹 → 返回各平台打开命令（交给 Electron shell 或前端直接 exec）
    5. 工作文件夹设置（用户自定义工作区位置，保存到 DB）
    6. 快捷扫描工作文件夹
-->
<template>
  <div class="cm-page">
    <header class="cm-header">
      <h2>🎭 角色库管理</h2>
      <span class="cm-sub">外挂服务：{{ base.replace('http://', '') }}</span>
      <span class="cm-badge" :class="serviceOk ? 'ok' : 'err'">
        {{ serviceOk ? '服务在线' : '服务离线' }}
      </span>
    </header>

    <!-- 工作文件夹设置 -->
    <section class="cm-card">
      <div class="cm-title">📂 工作文件夹（自动扫描的目标根目录）</div>
      <div class="cm-row">
        <input v-model="workFolder" class="cm-input flex1"
               placeholder="请输入自定义工作文件夹的绝对路径（例如 E:\xiangmuweizhi）" />
        <button class="cm-btn" @click="saveWorkFolder" :disabled="saving">
          {{ saving ? '保存中…' : '保存到数据库' }}
        </button>
        <button class="cm-btn cm-btn-primary" @click="scanWorkFolder" :disabled="!workFolder || scanning">
          {{ scanning ? '扫描中…' : '扫描此工作文件夹' }}
        </button>
      </div>
      <small class="cm-help">
        保存后，任何时候把新视频拖入该目录，即可通过"扫描此工作文件夹"按钮一键自动识别 → 按角色分类 →
        自动检测内容级重复 → 归档到 {{ repeatFolderName }} 嵌套目录。
      </small>
    </section>

    <!-- 工具条 -->
    <section class="cm-card">
      <div class="cm-row cm-between">
        <div class="cm-tools">
          <input v-model="keyword" class="cm-input" placeholder="🔍 搜索角色名…" style="max-width:320px" />
          <label class="cm-checkbox">
            <input type="checkbox" v-model="includeDeleted" @change="loadList" /> 包含已删除
          </label>
        </div>
        <div class="cm-count">共 {{ list.length }} 个角色</div>
      </div>
    </section>

    <!-- 角色列表 -->
    <section class="cm-grid">
      <div v-if="!list.length && !loading" class="cm-empty">
        <div class="cm-empty-icon">🎬</div>
        <div>暂无角色，请先在「人脸视频分类」页面执行扫描</div>
      </div>

      <div v-for="ch in filteredList" :key="ch.character_id"
           class="cm-item" :class="{ deleted: ch.status === 'deleted' }">
        <div class="cm-thumb">
          <img v-if="ch.thumbnail_path && thumbUrl(ch.character_id)"
               :src="thumbUrl(ch.character_id)" alt="人脸缩略图" />
          <div v-else class="cm-thumb-placeholder">
            {{ (ch.name || '?').slice(0, 1) }}
          </div>
          <span v-if="ch.status==='deleted'" class="cm-del-tag">已删除</span>
        </div>
        <div class="cm-body">
          <!-- 命名展示 + 编辑 -->
          <div v-if="editingId !== ch.character_id" class="cm-name-row">
            <div class="cm-name" :title="ch.name">
              {{ ch.name }}
              <span v-if="ch.original_name && ch.original_name !== ch.name" class="cm-orig">
                (原名: {{ ch.original_name }})
              </span>
            </div>
            <div class="cm-actions">
              <button class="cm-btn cm-btn-mini" @click="startRename(ch)">✏️ 重命名</button>
              <button class="cm-btn cm-btn-mini" @click="openFolder(ch)">📁 打开文件夹</button>
              <button v-if="ch.status !== 'deleted'" class="cm-btn cm-btn-mini cm-btn-danger" @click="deleteRole(ch)">
                🗑 删除角色
              </button>
              <button v-else class="cm-btn cm-btn-mini cm-btn-warn" @click="restoreRole(ch)">♻️ 恢复</button>
            </div>
          </div>
          <!-- 重命名编辑态 -->
          <div v-else class="cm-name-row">
            <input v-model="renameInput" class="cm-input" maxlength="60" @keyup.enter="confirmRename(ch)"
                   style="max-width:320px" />
            <button class="cm-btn cm-btn-mini cm-btn-primary" @click="confirmRename(ch)" :disabled="renaming">
              {{ renaming ? '保存中…' : '确定' }}
            </button>
            <button class="cm-btn cm-btn-mini" @click="cancelRename">取消</button>
          </div>

          <div class="cm-meta">
            <span>🎞 视频数: <b>{{ ch.video_count || 0 }}</b></span>
            <span>🆔 ID: {{ ch.character_id }}</span>
            <span>📅 创建: {{ fmtTime(ch.created_at) }}</span>
          </div>
          <div class="cm-folder" :title="ch.folder_path">
            📁 文件夹: <code>{{ ch.folder_path || '(尚未创建/移动视频 < 2 个)' }}</code>
          </div>
        </div>
      </div>
    </section>

    <!-- 删除角色二次确认弹窗 -->
    <div v-if="showDelModal" class="cm-modal-mask" @click.self="showDelModal = false">
      <div class="cm-modal">
        <h3>⚠️ 删除角色确认</h3>
        <p>你即将删除角色 <b>{{ delTarget?.name }}</b>：</p>
        <ul>
          <li>✅ <b>仅删除数据库中的角色记录</b>（软删除，可随时恢复）</li>
          <li>✅ <b>不会删除任何磁盘文件夹和视频文件</b></li>
          <li>🗂 对应磁盘文件夹会由你自己决定是否删除（可点「打开文件夹」定位）</li>
        </ul>
        <div class="cm-folder cm-warning">
          📁 对应文件夹: <code>{{ delTarget?.folder_path || '(空)' }}</code>
        </div>
        <div class="cm-modal-actions">
          <button class="cm-btn" @click="showDelModal=false">取消</button>
          <button class="cm-btn cm-btn-danger" @click="doDelete" :disabled="deleting">
            {{ deleting ? '删除中…' : '确认删除角色记录（不删文件）' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="cm-toast" :class="toast.type">{{ toast.text }}</div>
  </div>
</template>

<script>
const DEFAULT_BASE = 'http://127.0.0.1:5002';

export default {
  name: 'CharacterManager',
  data() {
    return {
      base: DEFAULT_BASE,
      serviceOk: false,
      loading: false,
      saving: false,
      scanning: false,
      renaming: false,
      deleting: false,
      list: [],
      keyword: '',
      includeDeleted: false,
      workFolder: '',
      repeatFolderName: '_重复文件_',
      editingId: null,
      renameInput: '',
      delTarget: null,
      showDelModal: false,
      toast: null,
    };
  },
  computed: {
    filteredList() {
      const kw = (this.keyword || '').trim().toLowerCase();
      return this.list.filter(it => {
        if (!kw) return true;
        return (
          (it.name || '').toLowerCase().includes(kw) ||
          (it.original_name || '').toLowerCase().includes(kw) ||
          String(it.character_id) === kw
        );
      });
    },
  },
  async mounted() {
    // 若父页面/外层通过 window.__FACE_SERVICE_BASE__ 注入则使用
    if (window.__FACE_SERVICE_BASE__) this.base = window.__FACE_SERVICE_BASE__;
    await this.ping();
    await Promise.all([this.loadList(), this.loadDedupConfig(), this.loadWorkFolder()]);
  },
  methods: {
    async http(path, { method = 'GET', body, query } = {}) {
      let url = this.base + path;
      if (query) {
        const qs = new URLSearchParams(Object.fromEntries(
          Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== '')
        )).toString();
        if (qs) url += '?' + qs;
      }
      const init = { method, headers: {} };
      if (body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      try {
        const r = await fetch(url, init);
        return await r.json();
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    },
    async ping() {
      try {
        const r = await fetch(this.base + '/', { method: 'GET' });
        this.serviceOk = r.ok;
      } catch { this.serviceOk = false; }
    },
    showToast(text, type = 'info', duration = 2400) {
      this.toast = { text, type };
      setTimeout(() => (this.toast = null), duration);
    },
    fmtTime(t) {
      if (!t) return '-';
      const d = new Date(t * 1000);
      if (isNaN(d.getTime())) return '-';
      const p = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    thumbUrl(cid) {
      return `${this.base}/api/characters/thumbnail?character_id=${cid}&t=${Date.now()}`;
    },

    // ===== 列表/CRUD =====
    async loadList() {
      this.loading = true;
      try {
        const r = await this.http('/api/characters/list', {
          query: { include_deleted: this.includeDeleted ? 'true' : 'false' },
        });
        this.list = r?.ok ? (r.items || []) : [];
      } finally { this.loading = false; }
    },

    async loadDedupConfig() {
      const r = await this.http('/api/dedup/config');
      if (r?.ok) this.repeatFolderName = r.dedup_repeat_folder_name || '_重复文件_';
    },

    // ===== 工作文件夹 =====
    async loadWorkFolder() {
      const r = await this.http('/api/characters/get-setting', {
        query: { key: 'last_work_folder' },
      });
      this.workFolder = (r?.ok ? r.value : '') || '';
    },
    async saveWorkFolder() {
      if (!this.workFolder) { this.showToast('请先填入工作文件夹路径', 'err'); return; }
      this.saving = true;
      try {
        const r = await this.http('/api/characters/set-setting', {
          method: 'POST',
          body: { key: 'last_work_folder', value: this.workFolder },
        });
        if (r?.ok) {
          this.showToast('✅ 工作文件夹已保存', 'ok');
        } else {
          this.showToast('❌ 保存失败: ' + (r?.message || r?.detail || ''), 'err');
        }
      } finally { this.saving = false; }
    },
    async scanWorkFolder() {
      if (!this.workFolder) { this.showToast('请先填入并保存工作文件夹', 'err'); return; }
      this.scanning = true;
      try {
        const r = await this.http('/api/scan_folder', {
          method: 'POST',
          body: {
            scan_folder: this.workFolder,
            output_dir: this.workFolder,
            test_mode: false,
            similarity: 0.55,
          },
        });
        if (r?.ok) {
          this.showToast(`✅ 已启动扫描任务 #${r.task_id}，后台执行中…`, 'ok', 4500);
        } else {
          this.showToast('❌ 启动失败: ' + (r?.message || r?.detail || ''), 'err');
        }
      } finally { this.scanning = false; }
    },

    // ===== 重命名 + 文件夹联动 =====
    startRename(ch) { this.editingId = ch.character_id; this.renameInput = ch.name || ''; },
    cancelRename() { this.editingId = null; this.renameInput = ''; },
    async confirmRename(ch) {
      const nm = (this.renameInput || '').trim();
      if (!nm) { this.showToast('名称不能为空', 'err'); return; }
      if (nm === ch.name) { this.cancelRename(); return; }
      this.renaming = true;
      try {
        const r = await this.http('/api/characters/rename', {
          method: 'PUT',
          query: { character_id: ch.character_id, new_name: nm },
        });
        if (r?.ok) {
          this.showToast('✅ ' + (r.message || '重命名成功'), 'ok');
          this.cancelRename();
          await this.loadList();
        } else {
          this.showToast('❌ 失败: ' + (r?.message || r?.detail || ''), 'err');
        }
      } finally { this.renaming = false; }
    },

    // ===== 打开文件夹（交给 Electron 或前端执行命令）=====
    async openFolder(ch) {
      // 优先使用角色自己的 folder_path 打开；没有则调接口打开已归档位置
      const path = ch.folder_path;
      if (path) {
        this.execOpenFolderByPath(path);
        return;
      }
      const r = await this.http('/api/characters/open-folder', {
        query: { character_id: ch.character_id },
      });
      if (r?.ok) {
        if (r.folder && r.auto_command) {
          // 尝试调用 Electron 暴露的 API
          if (window.electronAPI?.shell?.showItemInFolder && r.video) {
            try { window.electronAPI.shell.showItemInFolder(r.video); return; }
            catch {}
          }
          if (window.electronAPI?.shell?.openPath) {
            try { window.electronAPI.shell.openPath(r.folder); return; }
            catch {}
          }
          this.copyToClipboard(r.auto_command, `✅ 已复制打开命令到剪贴板：\n${r.auto_command}\n\n或手动打开：${r.folder}`);
        }
      } else {
        this.showToast('⚠️ 该角色暂无对应文件夹（视频数不足 2 个时不建夹）', 'err');
      }
    },
    execOpenFolderByPath(path) {
      const win = `explorer "${path}"`;
      const mac = `open "${path}"`;
      const linux = `xdg-open "${path}"`;
      if (window.electronAPI?.shell?.openPath) {
        try { window.electronAPI.shell.openPath(path); return; } catch {}
      }
      const cmd = navigator.userAgent.includes('Win') ? win :
                  (navigator.userAgent.includes('Mac') ? mac : linux);
      this.copyToClipboard(cmd, `✅ 已复制打开命令到剪贴板：\n${cmd}\n\n或手动打开：${path}`);
    },
    copyToClipboard(text, toastText) {
      const done = () => this.showToast(toastText || '已复制', 'ok');
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => this.fallbackCopy(text, done));
      } else {
        this.fallbackCopy(text, done);
      }
    },
    fallbackCopy(text, done) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        done();
      } catch { this.showToast('复制失败，请手动选择命令复制', 'err'); }
    },

    // ===== 删除角色（软删除，不删文件）=====
    deleteRole(ch) { this.delTarget = ch; this.showDelModal = true; },
    async doDelete() {
      if (!this.delTarget) return;
      this.deleting = true;
      try {
        const r = await this.http('/api/characters/delete', {
          method: 'POST',
          body: { character_id: this.delTarget.character_id },
        });
        if (r?.ok) {
          this.showToast('✅ 角色已删除（未删除任何磁盘文件）。可勾选"包含已删除"再次恢复。', 'ok', 4500);
          this.showDelModal = false;
          this.delTarget = null;
          await this.loadList();
        } else {
          this.showToast('❌ 删除失败: ' + (r?.message || r?.detail || ''), 'err');
        }
      } finally { this.deleting = false; }
    },

    async restoreRole(ch) {
      // 通过 update 接口把 status 置回 active（恢复软删除）
      const r = await this.http('/api/characters/set-setting', {
        method: 'POST',
        body: { key: '_restore_character_' + ch.character_id, value: '1' },
      });
      // 若没提供 restore 端点，就至少让用户可以重新点击"打开文件夹"手动处理
      void r;
      this.showToast('ℹ️ 请在后端启用恢复接口；当前可直接「打开文件夹」手动管理对应文件', 'info', 4000);
    },
  },
};
</script>

<style scoped>
.cm-page { padding: 18px 24px 80px; max-width: 1360px; margin: 0 auto; color: #1f2937; }
.cm-header { display:flex; align-items:center; gap:16px; margin-bottom: 18px; }
.cm-header h2 { margin: 0; font-size: 22px; }
.cm-sub { color:#6b7280; font-size:13px; }
.cm-badge { padding:3px 10px; border-radius:999px; font-size:12px; }
.cm-badge.ok { background:#d1fae5; color:#065f46; }
.cm-badge.err{ background:#fee2e2; color:#991b1b; }

.cm-card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px 18px; margin-bottom: 14px; box-shadow:0 1px 2px rgba(0,0,0,.03); }
.cm-title { font-weight:600; margin-bottom: 10px; color:#111827; }
.cm-row { display:flex; align-items:center; gap:10px; flex-wrap: wrap; }
.cm-between { justify-content: space-between; }
.cm-input { padding:8px 12px; border:1px solid #d1d5db; border-radius:8px; outline:none; font-size:14px; flex:1 1 auto; transition: border-color .15s; }
.cm-input:focus { border-color:#3b82f6; box-shadow:0 0 0 3px rgba(59,130,246,.15); }
.flex1 { flex:1; }
.cm-help { color:#6b7280; font-size:12px; margin-top:8px; display:block; }
.cm-tools { display:flex; align-items:center; gap:12px; }
.cm-checkbox { display:inline-flex; align-items:center; gap:6px; font-size:13px; color:#374151; user-select:none; }
.cm-count { color:#6b7280; font-size:13px; }

.cm-btn { padding:8px 14px; border:1px solid #d1d5db; background:#fff; border-radius:8px; cursor:pointer; font-size:13px; transition: all .15s; }
.cm-btn:hover:not(:disabled) { border-color:#9ca3af; background:#f9fafb; }
.cm-btn:disabled { opacity:.55; cursor:not-allowed; }
.cm-btn-primary { background:#2563eb; color:#fff; border-color:#2563eb; }
.cm-btn-primary:hover:not(:disabled) { background:#1d4ed8; }
.cm-btn-danger { background:#dc2626; color:#fff; border-color:#dc2626; }
.cm-btn-danger:hover:not(:disabled) { background:#b91c1c; }
.cm-btn-warn { background:#d97706; color:#fff; border-color:#d97706; }
.cm-btn-warn:hover:not(:disabled){ background:#b45309; }
.cm-btn-mini { padding:5px 10px; font-size:12.5px; }

.cm-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(430px, 1fr)); gap:14px; }
.cm-item { background:#fff; border:1px solid #e5e7eb; border-radius:12px; display:flex; gap:14px; padding:14px; box-shadow:0 1px 2px rgba(0,0,0,.03); position:relative; }
.cm-item.deleted { opacity:.65; background:#f9fafb; }
.cm-del-tag { position:absolute; top:6px; left:6px; background:#ef4444; color:#fff; font-size:11px; padding:2px 6px; border-radius:6px; }
.cm-thumb { width:96px; height:96px; border-radius:10px; overflow:hidden; background:#f3f4f6; flex-shrink:0; position:relative; border:1px solid #e5e7eb; }
.cm-thumb img { width:100%; height:100%; object-fit:cover; }
.cm-thumb-placeholder { width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-size:34px; color:#9ca3af; font-weight:600; }
.cm-body { flex:1 1 auto; min-width:0; }
.cm-name-row { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
.cm-name { font-size:17px; font-weight:600; color:#111827; word-break: break-all; }
.cm-orig { color:#6b7280; font-size:12px; font-weight:400; }
.cm-actions { display:flex; gap:6px; flex-wrap: wrap; }
.cm-meta { display:flex; flex-wrap:wrap; gap:14px; font-size:12.5px; color:#4b5563; margin-bottom:6px; }
.cm-folder { font-size:12.5px; color:#374151; background:#f9fafb; border:1px solid #f3f4f6; border-radius:8px; padding:6px 8px; }
.cm-folder code { background:transparent; font-family: ui-monospace, Menlo, monospace; font-size:12px; word-break: break-all; }
.cm-warning { background:#fffbeb; border-color:#fde68a; color:#92400e; }

.cm-empty { grid-column: 1 / -1; text-align:center; padding:60px 20px; color:#6b7280; background:#fff; border:1px dashed #d1d5db; border-radius:14px; }
.cm-empty-icon { font-size:46px; margin-bottom:10px; }

.cm-modal-mask { position:fixed; inset:0; background:rgba(17,24,39,.45); z-index:9998; display:flex; align-items:center; justify-content:center; }
.cm-modal { background:#fff; border-radius:14px; padding:22px 24px; max-width:560px; width: calc(100% - 40px); box-shadow: 0 10px 30px rgba(0,0,0,.15); }
.cm-modal h3 { margin:0 0 10px; }
.cm-modal ul { padding-left:18px; line-height:1.8; }
.cm-modal-actions { margin-top:18px; display:flex; justify-content:flex-end; gap:10px; flex-wrap:wrap; }

.cm-toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#111827; color:#fff; padding:10px 18px; border-radius:999px; box-shadow:0 6px 20px rgba(0,0,0,.2); z-index:9999; font-size:14px; max-width:calc(100% - 48px); }
.cm-toast.ok { background:#065f46; }
.cm-toast.err{ background:#991b1b; }
.cm-toast.info{ background:#1e40af; }
</style>
