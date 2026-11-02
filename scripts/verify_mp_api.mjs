/**
 * 小程序端 API 链路回归验证（零依赖，使用 Node 内置 fetch）
 *
 * 前置：先启动后端
 *   <venv>/Scripts/python.exe -m uvicorn guibeicai.api.main:app --host 127.0.0.1 --port 8000
 * 运行：
 *   node scripts/verify_mp_api.mjs
 *
 * 覆盖：健康检查 / 登录鉴权（正确密码+错误密码+无令牌+伪造令牌）/ 门店数据隔离 /
 *       预测三维度接口（总览、单菜品、食材）/ 对比接口 / 无预测的 404 降级
 */
const BASE = 'http://127.0.0.1:8000'
let pass = 0
let fail = 0

function ok(name, cond, extra) {
  if (cond) {
    pass++
    console.log('  ✓', name, extra ? '— ' + extra : '')
  } else {
    fail++
    console.log('  ✗', name, extra ? '— ' + extra : '')
  }
}

async function call(method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = 'Bearer ' + token
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined })
  let data = null
  try {
    data = await r.json()
  } catch (e) {
    data = null
  }
  return { status: r.status, data }
}

;(async () => {
  console.log('【1】健康检查')
  {
    const r = await call('GET', '/api/health')
    ok('GET /api/health 返回 200', r.status === 200, JSON.stringify(r.data))
  }

  console.log('\n【2】错误密码应被拒绝')
  {
    const r = await call('POST', '/api/login', { username: 'admin', password: 'wrong-pwd' })
    ok('错误密码返回 401', r.status === 401, 'status=' + r.status)
  }

  console.log('\n【3】管理员登录 admin/admin')
  let token = ''
  let adminStores = []
  {
    const r = await call('POST', '/api/login', { username: 'admin', password: 'admin' })
    ok('登录返回 200', r.status === 200)
    ok('返回 token', !!(r.data && r.data.token))
    ok('返回门店列表', Array.isArray(r.data && r.data.stores), '门店数=' + (r.data?.stores?.length ?? 0))
    ok('is_admin = true', r.data?.user?.is_admin === true, 'role=' + r.data?.user?.role)
    token = r.data?.token || ''
    adminStores = r.data?.stores || []
  }

  console.log('\n【4】带令牌访问受保护接口')
  {
    const noTok = await call('GET', '/api/me')
    ok('无令牌 /api/me 返回 401', noTok.status === 401)

    const fake = await call('GET', '/api/me', null, 'tampered.token.value')
    ok('伪造令牌 /api/me 返回 401', fake.status === 401)

    const me = await call('GET', '/api/me', null, token)
    ok('正常令牌 /api/me 返回 200', me.status === 200, 'user=' + me.data?.username)

    const st = await call('GET', '/api/stores', null, token)
    ok('GET /api/stores 返回门店', st.status === 200 && st.data.length > 0, st.data.length + ' 家')
  }

  console.log('\n【5】预测相关接口（取第 1 家门店）')
  const storeId = adminStores[0]?.id
  if (!storeId) {
    ok('存在可测试门店', false, '无门店')
  } else {
    ok('取到测试门店', true, 'store_id=' + storeId)

    const pd = await call('GET', `/api/prediction-dates?store_id=${storeId}`, null, token)
    ok('prediction-dates 返回 200', pd.status === 200, '已有预测 ' + (pd.data?.dates?.length ?? 0) + ' 天')

    const dates = pd.data?.dates || []
    if (dates.length) {
      const d = dates[dates.length - 1]
      const pred = await call('GET', `/api/prediction?store_id=${storeId}&date=${d}`, null, token)
      ok('prediction 返回 200', pred.status === 200, d)

      const dishes = await call('GET', `/api/prediction/${d}/dishes?store_id=${storeId}`, null, token)
      ok('dishes 返回 200', dishes.status === 200, (dishes.data?.length ?? 0) + ' 个菜品')

      const ings = await call('GET', `/api/prediction/${d}/ingredients?store_id=${storeId}`, null, token)
      ok('ingredients 返回 200', ings.status === 200, (ings.data?.items?.length ?? 0) + ' 项食材, 合计 ¥' + ings.data?.total_cost)

      const cmp = await call('GET', `/api/compare?store_id=${storeId}&date=${d}`, null, token)
      ok('compare 返回 200', cmp.status === 200, (cmp.data?.rows?.length ?? 0) + ' 行')
    } else {
      const pred = await call('GET', `/api/prediction?store_id=${storeId}&date=2026-09-04`, null, token)
      ok('无预测日期时返回 404（前端会显示「暂无预测」）', pred.status === 404, 'status=' + pred.status)
    }
  }

  console.log('\n【6】商家账号 test/test（非管理员，应被门店隔离）')
  {
    const r = await call('POST', '/api/login', { username: 'test', password: 'test' })
    ok('商家登录 200', r.status === 200, 'is_admin=' + r.data?.user?.is_admin + ', 门店数=' + (r.data?.stores?.length ?? 0))
    const t2 = r.data?.token || ''
    if (t2 && adminStores.length > 1) {
      const other = adminStores.find((s) => !(r.data.stores || []).some((x) => x.id === s.id))
      if (other) {
        const denied = await call('GET', `/api/stores`, null, t2)
        ok('商家 /api/stores 只返回绑定门店', denied.status === 200 && denied.data.length === (r.data.stores || []).length, denied.data.length + ' 家')
        const pd2 = await call('GET', `/api/prediction-dates?store_id=${other.id}`, null, t2)
        ok('越权访问他人门店返回 403', pd2.status === 403, 'status=' + pd2.status)
      }
    }
  }

  console.log('\n===== 通过 ' + pass + ' / 失败 ' + fail + ' =====')
  process.exit(fail ? 1 : 0)
})()
