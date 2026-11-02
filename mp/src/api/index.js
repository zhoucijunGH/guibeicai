import { get, post } from './request'

/**
 * 桂备菜小程序端 API 清单（与 guibeicai/api/main.py 一一对应）
 * 所有接口除 /api/login 外均需 Authorization: Bearer <token>（由 request.js 自动注入）
 */

export const api = {
  // ---------- 鉴权 ----------
  /** 登录：复用 WEB 端 users 表账户 */
  login: (username, password) =>
    post('/api/login', { username, password }, { auth: false, loading: true, loadingText: '登录中' }),
  /** 令牌自检 / 获取用户信息 */
  me: () => get('/api/me', {}, { silent: true }),
  /** 可访问门店列表 */
  stores: () => get('/api/stores', {}, { silent: true }),

  // ---------- 微信登录 / 绑定（后端 /api/wx/*；手机号绑定，openid 全程自动） ----------
  /** 微信 code2session 一键登录：code 由 wx.login() 获取（后端为 GET query） */
  wxLogin: (code) => get('/api/wx/login', { code }, { auth: false, loading: true, loadingText: '微信登录中' }),
  /** 按手机号绑定微信并直接登录：code=wx.login()，phoneCode=getPhoneNumber 组件 code（授权优先），phone=手输兜底 */
  wxLoginByPhone: (code, phone, phoneCode) =>
    post('/api/wx/bind-phone', { code, phone, phone_code: phoneCode }, { auth: false, loading: true, loadingText: '绑定中' }),
  /** 把当前账户的 openid 绑定（推送身份识别；一般由上面的手机号绑定自动完成） */
  wxBind: (code) => post('/api/wx/bind', { code }, { loading: true, loadingText: '绑定中' }),
  /** 解除 openid 绑定 */
  wxUnbind: () => post('/api/wx/unbind', {}, { loading: true, loadingText: '解绑中' }),
  // ---------------------------------------------------------------------------------------

  // ---------- 采购单移动推送 ----------
  /** 生成并推送采购单（邮件/企微/微信订阅消息） */
  pushPurchaseOrder: (storeId, date) =>
    post('/api/push/purchase-order', { store_id: storeId, date: date || null }, { loading: true, loadingText: '推送中' }),

  // ---------- 预测查询 ----------
  /** 该门店已有预测的日期列表 */
  predictionDates: (storeId) => get('/api/prediction-dates', { store_id: storeId }, { silent: true }),
  /** 菜品菜单（id+name+category），供录入选菜/回填 */
  dishesList: (storeId) => get('/api/dishes', { store_id: storeId }, { silent: true }),
  /** 某日预测总览（财务 + 风险 + dish_predictions） */
  prediction: (storeId, date) => get('/api/prediction', { store_id: storeId, date }, { silent: true }),
  /** 单菜品维度：每菜预测销量 + 其食材采购需求 */
  dishes: (storeId, date) => get(`/api/prediction/${date}/dishes`, { store_id: storeId }, { silent: true }),
  /** 食材维度：当日采购量 + 金额汇总（按成本降序） */
  ingredients: (storeId, date) =>
    get(`/api/prediction/${date}/ingredients`, { store_id: storeId }, { silent: true }),
  /** 预测 vs 实际对比 */
  compare: (storeId, date) => get('/api/compare', { store_id: storeId, date }, { silent: true }),

  // ---------- 写操作 ----------
  /** 触发预测（异步） */
  runPredict: (storeId, date, specialEvent) =>
    post(
      '/api/predict/run',
      { store_id: storeId, predict_date: date, special_event: specialEvent || '' },
      { loading: true, loadingText: '提交中' }
    ),
  /** 预测任务进度 */
  predictStatus: (taskId) => get(`/api/predict/status/${taskId}`, {}, { silent: true }),
  /** 录入/更新实际销量：items = [{ dish_id, actual_qty, actual_waste }] */
  saveActualSales: (storeId, date, items) =>
    post(
      '/api/actual-sales',
      { store_id: storeId, sale_date: date, items },
      { loading: true, loadingText: '保存中' }
    ),

  // ---------- 健康检查（无需鉴权） ----------
  health: () => get('/api/health', {}, { auth: false, silent: true }),
}

export default api
