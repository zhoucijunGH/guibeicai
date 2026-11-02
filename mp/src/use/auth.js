import { onShow } from '@dcloudio/uni-app'
import { isLoggedIn } from '../store'

/**
 * 页面登录守卫：无令牌时回登录页。
 * 用于 tabBar 页面（登录页不使用）。
 */
export function useAuthGuard() {
  onShow(() => {
    if (!isLoggedIn()) {
      uni.reLaunch({ url: '/pages/login/login' })
    }
  })
}

/** 统一的数据加载错误提示 */
export function errText(e, fallback) {
  if (!e) return fallback || '加载失败'
  if (e.code === 404) return '该日暂无预测结果'
  if (e.code === 403) return '无权访问该门店数据'
  if (e.code === 401) return '登录已过期'
  if (e.code === 0) return '连不上后端服务，请确认 API 已启动'
  return e.message || fallback || '加载失败'
}
