import { state, clearAuth } from '../store'
import { config, REQUEST_TIMEOUT } from '../config'

/**
 * 统一网络层：
 * - 自动拼接后端基址（config.apiBase，可在登录页修改）
 * - 自动注入 Authorization: Bearer <token>
 * - 401 → 清理登录态并跳转登录页（避免重复跳转）
 * - 连接失败 → 给出「后端未启动」的可执行提示
 */

export class ApiError extends Error {
  constructor(code, message, data) {
    super(message)
    this.code = code
    this.data = data
  }
}

let jumping = false

function toast(msg) {
  uni.showToast({ title: String(msg || '').slice(0, 40), icon: 'none', duration: 2400 })
}

function currentRoute() {
  try {
    const pages = getCurrentPages()
    if (pages && pages.length) {
      const p = pages[pages.length - 1]
      return p.route || (p.$page && p.$page.fullPath) || ''
    }
  } catch (e) {
    /* ignore */
  }
  return ''
}

/** 登录过期：清空态 + 跳登录页 */
function handle401() {
  clearAuth()
  const route = currentRoute()
  if (route.indexOf('pages/login/login') >= 0) return
  if (jumping) return
  jumping = true
  uni.showToast({ title: '登录已过期，请重新登录', icon: 'none', duration: 1600 })
  uni.reLaunch({
    url: '/pages/login/login',
    complete: () => {
      setTimeout(() => {
        jumping = false
      }, 800)
    },
  })
}

function fullUrl(url) {
  if (/^https?:\/\//i.test(url)) return url
  const base = (config.apiBase || '').replace(/\/+$/, '')
  return base + (url.startsWith('/') ? url : '/' + url)
}

/**
 * @param {Object} opts
 * @param {string} opts.url        接口路径或完整 URL
 * @param {string} [opts.method]   GET / POST
 * @param {Object} [opts.data]     请求参数（GET 会拼 query）
 * @param {boolean} [opts.auth]    是否携带令牌，默认 true
 * @param {boolean} [opts.loading] 是否显示 loading 遮罩
 * @param {boolean} [opts.silent]  失败时不弹 toast（由调用方自行处理）
 */
export function request(opts) {
  const {
    url,
    method = 'GET',
    data,
    auth = true,
    loading = false,
    loadingText = '加载中',
    silent = false,
  } = opts || {}

  return new Promise((resolve, reject) => {
    if (loading) uni.showLoading({ title: loadingText, mask: true })

    const header = { 'Content-Type': 'application/json' }
    if (auth && state.token) header['Authorization'] = 'Bearer ' + state.token

    uni.request({
      url: fullUrl(url),
      method: method.toUpperCase(),
      data: data || {},
      header,
      timeout: REQUEST_TIMEOUT,
      success: (res) => {
        if (loading) uni.hideLoading()
        const code = res.statusCode
        const body = res.data

        if (code === 401) {
          handle401()
          reject(new ApiError(401, '登录已过期，请重新登录'))
          return
        }
        if (code === 404) {
          const msg = (body && (body.detail || body.message)) || '数据不存在'
          reject(new ApiError(404, msg, body))
          return
        }
        if (code === 403) {
          const msg = (body && (body.detail || body.message)) || '无权访问该数据'
          if (!silent) toast(msg)
          reject(new ApiError(403, msg, body))
          return
        }
        if (code === 422 || code === 400) {
          const msg = (body && (body.detail || body.message)) || '请求参数有误'
          if (!silent) toast(msg)
          reject(new ApiError(code, msg, body))
          return
        }
        if (code >= 500) {
          const msg = '服务异常（' + code + '）'
          if (!silent) toast(msg)
          reject(new ApiError(code, msg, body))
          return
        }
        resolve(body)
      },
      fail: (err) => {
        if (loading) uni.hideLoading()
        const raw = (err && (err.errMsg || err.message)) || '网络请求失败'
        if (!silent) {
          if (/timeout/i.test(raw)) toast('请求超时，请稍后重试')
          else toast('连不上后端服务，请确认 API 已启动（' + config.apiBase + '）')
        }
        reject(new ApiError(0, raw))
      },
    })
  })
}

export const get = (url, params, opts) => request({ url, method: 'GET', data: params, ...opts })
export const post = (url, data, opts) => request({ url, method: 'POST', data, ...opts })

/** 轮询辅助：间隔 ms 执行 fn，返回 true 时提前结束 */
export function poll(fn, interval = 1500, maxTimes = 120) {
  return new Promise((resolve, reject) => {
    let n = 0
    const timer = setInterval(async () => {
      n += 1
      try {
        const done = await fn()
        if (done) {
          clearInterval(timer)
          resolve()
        } else if (n >= maxTimes) {
          clearInterval(timer)
          reject(new ApiError(408, '等待超时'))
        }
      } catch (e) {
        clearInterval(timer)
        reject(e)
      }
    }, interval)
  })
}
