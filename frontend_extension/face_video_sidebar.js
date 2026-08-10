/**
 * 人脸视频分类 - 侧边栏菜单配置（新增文件，不改动原有菜单）
 *
 * ──────────── 集成方式（仅需在萤核 sidebar.js 追加一行）────────────
 * 在 src-frontend/src/layout/sidebar.js 顶部追加：
 *
 *   import { faceVideoMenu } from '../../../frontend_extension/face_video_sidebar.js'
 *
 * 并在原有菜单数组里展开（仅这一处合并，不修改原菜单项）：
 *
 *   const menus = [ ...原菜单, ...faceVideoMenu ]
 *
 * 或最小侵入写法（仅追加一条）：
 *   menus.push(faceVideoMenu[0])
 *
 * 注：路径以萤核实际工程结构为准；若使用 Vite 别名 @，可写
 *   import { faceVideoMenu } from '@/../frontend_extension/face_video_sidebar.js'
 * 并在 vite.config 中将 frontend_extension 纳入可解析范围。
 * ────────────────────────────────────────────────────────────
 */

// 使用动态 import 加载外部独立页面（懒加载，不污染萤核路由表）
const FaceVideoClassify = () => import('./views/FaceVideoClassify.vue')

export const faceVideoMenu = [
  {
    path: '/face-video-classify',
    name: 'FaceVideoClassify',
    component: FaceVideoClassify,
    meta: {
      title: '人脸视频分类',
      icon: 'face',          // 复用萤核已有图标名；若无该图标可改为存在的图标
      requiresAuth: false,
    },
  },
]

export default faceVideoMenu
