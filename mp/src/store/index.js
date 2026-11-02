import { reactive } from 'vue'
import { KEYS, loadApiBase } from '../config'

/** 全局状态：登录态 / 门店 / 日期 / 主题。持久化到 storage，跨端通用。 */

function pad(n) {
  return n < 10 ? '0' + n : '' + n
}

export function fmtDate(d) {
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
}

export function today() {
  return fmtDate(new Date())
}

export function shiftDate(str, days) {
  const parts = String(str || today()).split('-').map(Number)
  const dt = new Date(parts[0], (parts[1] || 1) - 1, parts[2] || 1)
  dt.setDate(dt.getDate() + days)
  return fmtDate(dt)
}

export const state = reactive({
  token: '',
  user: null, // { id, username, role, is_admin, store_ids }
  stores: [], // 可访问门店列表
  currentStoreId: null,
  currentDate: today(),
  theme: 'light',
  predictionDates: [], // 当前门店已有预测的日期
})

function read(key, fallback) {
  try {
    const v = uni.getStorageSync(key)
    return v === '' || v === null || v === undefined ? fallback : v
  } catch (e) {
    return fallback
  }
}

function write(key, value) {
  try {
    uni.setStorageSync(key, value)
  } catch (e) {
    /* ignore */
  }
}

function remove(key) {
  try {
    uni.removeStorageSync(key)
  } catch (e) {
    /* ignore */
  }
}

/** 启动时从本地缓存恢复状态 */
export function loadStore() {
  loadApiBase()
  state.token = read(KEYS.token, '')
  state.user = read(KEYS.user, null)
  const stores = read(KEYS.stores, [])
  state.stores = Array.isArray(stores) ? stores : []
  const sid = read(KEYS.storeId, null)
  state.currentStoreId = sid != null ? Number(sid) : state.stores[0] ? state.stores[0].id : null
  state.currentDate = read(KEYS.date, today())
  state.theme = read(KEYS.theme, '')
}

export function saveAuth(token, user, stores) {
  state.token = token
  state.user = user || null
  state.stores = Array.isArray(stores) ? stores : []
  write(KEYS.token, token)
  write(KEYS.user, state.user)
  write(KEYS.stores, state.stores)
  if (state.currentStoreId == null || !state.stores.some((s) => s.id === state.currentStoreId)) {
    state.currentStoreId = state.stores[0] ? state.stores[0].id : null
    write(KEYS.storeId, state.currentStoreId)
  }
}

export function clearAuth() {
  state.token = ''
  state.user = null
  state.stores = []
  state.currentStoreId = null
  state.predictionDates = []
  ;[KEYS.token, KEYS.user, KEYS.stores, KEYS.storeId].forEach(remove)
}

export function isLoggedIn() {
  return !!state.token
}

export function setStore(id) {
  state.currentStoreId = id == null ? null : Number(id)
  write(KEYS.storeId, state.currentStoreId)
}

export function currentStore() {
  if (state.currentStoreId == null) return null
  return state.stores.find((s) => s.id === state.currentStoreId) || null
}

export function setDate(d) {
  state.currentDate = d
  write(KEYS.date, d)
}

export function setPredictionDates(dates) {
  state.predictionDates = Array.isArray(dates) ? dates : []
}

/** 主题：'light' | 'dark'。空值表示跟随系统，由 detectTheme 决定。 */
export function detectTheme() {
  // #ifdef H5
  try {
    if (typeof window !== 'undefined' && window.matchMedia) {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    }
  } catch (e) {
    /* ignore */
  }
  // #endif
  try {
    const si = uni.getSystemInfoSync()
    if (si && si.theme) return si.theme === 'dark' ? 'dark' : 'light'
  } catch (e) {
    /* ignore */
  }
  return 'light'
}

// H5（浏览器）有 window/document，微信小程序没有 —— 用此区分是否调用 uni 原生 API。
// H5 下 uni.setTabBarStyle / setNavigationBarColor 是空操作且会触发 uni 内部异常，
// 因此 H5 只用 DOM（全局 CSS）驱动主题，原生平台才走 uni API。
const isH5 = typeof window !== 'undefined' && typeof document !== 'undefined'

function applyThemeClass(theme) {
  // 给 <html> 打 theme-dark/theme-light 类，让全局 CSS（含 Tab 栏）跟随主题
  try {
    const el = document.documentElement
    if (!el) return
    if (theme === 'dark') {
      el.classList.add('theme-dark')
      el.classList.remove('theme-light')
    } else {
      el.classList.add('theme-light')
      el.classList.remove('theme-dark')
    }
  } catch (e) {
    /* ignore */
  }
}

function applyNavBar(theme) {
  if (isH5) return
  try {
    uni.setNavigationBarColor({
      frontColor: theme === 'dark' ? '#ffffff' : '#000000',
      backgroundColor: theme === 'dark' ? '#1b1f26' : '#ffffff',
    })
  } catch (e) {
    /* ignore */
  }
}

function applyTabBarNative(theme) {
  try {
    uni
      .setTabBarStyle({
        color: theme === 'dark' ? '#6b7684' : '#8a919f',
        selectedColor: theme === 'dark' ? '#f07b7a' : '#e15554',
        backgroundColor: theme === 'dark' ? '#1b1f26' : '#ffffff',
        borderStyle: theme === 'dark' ? 'black' : 'white',
      })
      .catch(() => {})
  } catch (e) {
    /* ignore */
  }
}

export function applyTabBar(theme) {
  applyThemeClass(theme)
  if (isH5) return
  applyTabBarNative(theme)
}

export function initTheme() {
  if (!state.theme) state.theme = detectTheme()
  // 启动即应用：html 主题类（驱动 CSS / Tab 栏）+ 导航栏
  applyThemeClass(state.theme)
  applyNavBar(state.theme)
}

export function setTheme(t) {
  state.theme = t
  write(KEYS.theme, t)
  applyThemeClass(t)
  applyNavBar(t)
  applyTabBar(t)
}

export function toggleTheme() {
  setTheme(state.theme === 'dark' ? 'light' : 'dark')
}
