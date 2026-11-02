import { reactive } from 'vue'

/**
 * 后端 API 基址。
 * - 本机 H5 调试：http://127.0.0.1:8000（默认）
 * - 微信开发者工具 / 真机：需改为电脑的局域网 IP，例如 http://192.168.1.20:8000
 *   可在登录页「服务器设置」里直接改，会缓存到本地。
 */
export const DEFAULT_API_BASE = 'http://127.0.0.1:8000'

/**
 * 登录模式：
 * - 'wechat' ：纯微信场景（默认），隐藏 WEB 账号/密码表单，仅保留微信一键登录 + 服务器设置。
 *              首次使用未绑定时，引导用手机号（微信授权或手输）绑定账号。
 * - 'both'   ：同时显示「账号密码登录」与「微信一键登录」（管理员排障/多端场景备用）。
 * 切换无需改后端，改这里重新构建小程序即可。
 */
export const LOGIN_MODE = 'wechat'

/** 本地缓存键名 */
export const KEYS = {
  token: 'gbc_token',
  user: 'gbc_user',
  stores: 'gbc_stores',
  storeId: 'gbc_store_id',
  date: 'gbc_date',
  theme: 'gbc_theme',
  apiBase: 'gbc_api_base',
}

export const config = reactive({
  apiBase: DEFAULT_API_BASE,
})

export function loadApiBase() {
  try {
    config.apiBase = uni.getStorageSync(KEYS.apiBase) || DEFAULT_API_BASE
  } catch (e) {
    config.apiBase = DEFAULT_API_BASE
  }
}

export function setApiBase(v) {
  const s = String(v || '').trim().replace(/\/+$/, '')
  config.apiBase = s || DEFAULT_API_BASE
  try {
    uni.setStorageSync(KEYS.apiBase, config.apiBase)
  } catch (e) {
    /* ignore */
  }
}

export const REQUEST_TIMEOUT = 30000

/**
 * 微信订阅消息模板 ID（移动端采购单推送）。
 * 在微信公众平台「订阅消息」中申请模板后，把模板 ID 填到这里（或改成从后端下发）。
 * 为空时「订阅推送」按钮会提示「未配置模板」，不影响其他功能。
 */
export const WX_SUBSCRIBE_TMPL_ID = ''
