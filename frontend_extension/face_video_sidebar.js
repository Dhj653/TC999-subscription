/**
 * 萤核侧边栏扩展（仅新增两个菜单项，不修改原有其他项）。
 *
 * 使用方法（Windows一键脚本会自动执行）：
 *   1. 将此文件内容合并到萤核：firefly-ai-folder-cn/src-frontend/src/layout/sidebar.js
 *      （或 patch_sidebar.py 自动注入）
 *   2. 在 router/index.js 中注册对应路由：
 *        /face-video        → FaceVideoClassify.vue
 *        /character-manager → CharacterManager.vue
 *
 * 本文件中的 sidebarExtensions 为新增项的定义，方便 patcher 读取。
 */

// 定义新增菜单项（patcher 会自动把 children 追加到萤核原有 routes）
export const sidebarExtensions = [
  {
    title: '人脸视频分类',
    icon: 'VideoIcon',
    path: '/face-video',
    order: 900, // 放在比较靠后的位置，不干扰原有菜单
    component: 'FaceVideoClassify',
  },
  {
    title: '角色管理',
    icon: 'UsersIcon',
    path: '/character-manager',
    order: 901,
    component: 'CharacterManager',
  },
]

// 对于原始 sidebar.js 如果是数组导出的格式，可以直接 concat
// 示例：
//   import { sidebarExtensions } from './face_video_sidebar'
//   export default [ ...originalRoutes, ...sidebarExtensions ]
export default sidebarExtensions
