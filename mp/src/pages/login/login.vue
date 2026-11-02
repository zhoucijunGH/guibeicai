<template>
  <view class="page" :class="'theme-' + state.theme">
    <view :style="{ height: statusBarHeight + 'px' }"></view>

    <view class="brand">
      <view class="brand-logo">桂</view>
      <view class="brand-name">桂备菜</view>
      <view class="brand-sub">中小餐饮 · 智能备菜</view>
    </view>

    <view class="form card">
      <template v-if="LOGIN_MODE !== 'wechat'">
        <view class="field">
          <text class="f-label">账号</text>
          <input
            class="f-input"
            v-model="username"
            placeholder="请输入 WEB 端账号"
            placeholder-class="f-ph"
            :adjust-position="true"
            confirm-type="next"
          />
        </view>
        <view class="field">
          <text class="f-label">密码</text>
          <input
            class="f-input"
            v-model="password"
            type="password"
            placeholder="请输入密码"
            placeholder-class="f-ph"
            :adjust-position="true"
            confirm-type="done"
            @confirm="doLogin"
          />
        </view>

        <view class="btn" :class="{ 'btn-disabled': loading }" @click="doLogin">
          {{ loading ? '登录中…' : '登 录' }}
        </view>

        <view class="hint tip">
          账号与权限完全复用 WEB 管理端（users 表 + 门店绑定）。
          普通用户只能看到自己绑定的门店，管理员可见全部门店。
        </view>
      </template>

      <!-- 微信一键登录（手机号绑定为主，openid 全程自动） -->
      <view class="btn btn-wx" @click="doWxLogin">
        <text class="wx-ico">💬</text> 微信一键登录
      </view>

      <!-- 未绑定：手机号绑定面板（微信授权优先，手动输入兜底） -->
      <view v-if="needBind" class="bind-box">
        <view class="bind-title">首次使用，请绑定手机号</view>
        <view class="bind-tip">用管理员登记的手机号匹配您的账号；绑定后即可微信一键登录，无需再输。</view>

        <button class="btn btn-wx" open-type="getPhoneNumber" @getphonenumber="onGetPhone">
          一键绑定手机号（微信授权）
        </button>

        <view class="bind-or">或 手动输入手机号</view>
        <input
          class="f-input"
          v-model="bindPhone"
          type="number"
          maxlength="11"
          placeholder="请输入 11 位手机号"
          placeholder-class="f-ph"
        />
        <view class="btn" :class="{ 'btn-disabled': loading }" @click="doBindByPhone">
          {{ loading ? '绑定中…' : '绑 定' }}
        </view>
      </view>
    </view>

    <view class="card">
      <view class="card-title" @click="showServer = !showServer">
        <text>服务器设置</text>
        <text class="sub">{{ showServer ? '收起 ▲' : '展开 ▾' }}</text>
      </view>
      <template v-if="showServer">
        <view class="field">
          <text class="f-label">API 地址</text>
          <input
            class="f-input"
            v-model="apiBase"
            placeholder="http://127.0.0.1:8000"
            placeholder-class="f-ph"
          />
        </view>
        <view class="row">
          <text class="label">连通状态</text>
          <text class="value" :class="healthOk ? 'ok' : 'bad'">
            {{ healthText }}
          </text>
        </view>
        <view class="btn btn-sub" @click="checkHealth">测试连接</view>
        <view class="hint tip">
          后端启动：<text class="mono">uvicorn guibeicai.api.main:app --port 8000</text>
          <br />
          微信开发者工具 / 真机调试请把 127.0.0.1 换成电脑的局域网 IP。
        </view>
      </template>
    </view>
  </view>
</template>

<script setup>
import { ref, computed } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import { state, isLoggedIn, clearAuth, saveAuth } from '../../store'
import { config, setApiBase, LOGIN_MODE } from '../../config'
import { api } from '../../api'

const username = ref('')
const password = ref('')
const needBind = ref(false)   // 微信未绑定账号时展示手机号绑定面板
const bindPhone = ref('')     // 手动输入手机号
const loading = ref(false)
const showServer = ref(false)
const apiBase = ref(config.apiBase)
const statusBarHeight = ref(20)
const healthState = ref('unknown') // unknown | ok | fail

const healthOk = computed(() => healthState.value === 'ok')
const healthText = computed(() =>
  healthState.value === 'ok' ? '已连通' : healthState.value === 'fail' ? '连不上' : '未测试'
)

onLoad(() => {
  try {
    const si = uni.getSystemInfoSync()
    statusBarHeight.value = si.statusBarHeight || 20
  } catch (e) {
    /* ignore */
  }

  // 已有令牌：先自检，通过则直接进首页，失效则清理并留在登录页
  if (isLoggedIn()) {
    api
      .me()
      .then(() => {
        uni.switchTab({ url: '/pages/index/index' })
      })
      .catch(() => {
        clearAuth()
      })
  }
})

async function checkHealth() {
  setApiBase(apiBase.value)
  healthState.value = 'unknown'
  try {
    await api.health()
    healthState.value = 'ok'
    uni.showToast({ title: '连接正常', icon: 'success' })
  } catch (e) {
    healthState.value = 'fail'
    uni.showToast({ title: '连不上：' + config.apiBase, icon: 'none' })
  }
}

async function doLogin() {
  if (loading.value) return
  const u = String(username.value || '').trim()
  const p = String(password.value || '')
  if (!u || !p) {
    uni.showToast({ title: '请输入账号和密码', icon: 'none' })
    return
  }

  setApiBase(apiBase.value)
  loading.value = true
  try {
    const res = await api.login(u, p)
    saveAuth(res.token, res.user, res.stores)
    // 登录成功后自动绑定微信 openid（用于采购单推送）；未配置 AppID 或用户拒绝则静默跳过
    tryBindWx()
    if (!res.stores || !res.stores.length) {
      uni.showToast({ title: '该账号未绑定门店，请先在 WEB 端配置', icon: 'none', duration: 2600 })
    }
    uni.switchTab({ url: '/pages/index/index' })
  } catch (e) {
    uni.showToast({
      title: e && e.code === 401 ? '账号或密码错误' : (e && e.message) || '登录失败',
      icon: 'none',
      duration: 2400,
    })
  } finally {
    loading.value = false
  }
}

// 微信一键登录：未绑定（401）时切换到手机号绑定面板
async function doWxLogin() {
  try {
    const { code } = await uni.login({ provider: 'weixin' })
    const res = await api.wxLogin(code)
    saveAuth(res.token, res.user, res.stores)
    uni.switchTab({ url: '/pages/index/index' })
  } catch (e) {
    if (e && e.code === 401) {
      needBind.value = true
      uni.showToast({ title: '首次使用，请先绑定手机号', icon: 'none' })
    } else {
      uni.showToast({ title: '微信登录失败：' + ((e && e.message) || ''), icon: 'none' })
    }
  }
}

// 微信授权手机号 → 绑定（getPhoneNumber 组件回调）
async function onGetPhone(e) {
  const d = (e && e.detail) || {}
  if (!d.code) {
    uni.showToast({ title: '已取消或未授权手机号', icon: 'none' })
    return
  }
  await bindWx(d.code, '')
}

// 手动输入手机号 → 绑定
async function doBindByPhone() {
  const p = String(bindPhone.value || '').trim()
  if (!/^\d{11}$/.test(p)) {
    uni.showToast({ title: '请输入 11 位手机号', icon: 'none' })
    return
  }
  await bindWx('', p)
}

// 统一绑定入口：phoneCode（授权）优先，phone（手输）兜底；成功后直接登录
async function bindWx(phoneCode, phone) {
  if (loading.value) return
  setApiBase(apiBase.value)
  loading.value = true
  try {
    const { code } = await uni.login({ provider: 'weixin' })
    const res = await api.wxLoginByPhone(code, phone, phoneCode)
    saveAuth(res.token, res.user, res.stores)
    needBind.value = false
    if (!res.stores || !res.stores.length) {
      uni.showToast({ title: '该账号未绑定门店，请先在 WEB 端配置', icon: 'none', duration: 2600 })
    } else {
      uni.showToast({ title: '绑定成功', icon: 'success' })
    }
    uni.switchTab({ url: '/pages/index/index' })
  } catch (e) {
    uni.showToast({ title: (e && e.message) || '绑定失败', icon: 'none', duration: 2600 })
  } finally {
    loading.value = false
  }
}

// 登录后尝试绑定微信 openid（推送前提）；失败不影响登录
async function tryBindWx() {
  try {
    const { code } = await uni.login({ provider: 'weixin' })
    await api.wxBind(code)
  } catch (e) {
    // 静默：未配置 AppID / 用户取消 等情况不阻断
  }
}
</script>

<style scoped>
.brand {
  padding: 60rpx 0 48rpx;
  text-align: center;
}

.brand-logo {
  width: 128rpx;
  height: 128rpx;
  line-height: 128rpx;
  margin: 0 auto 24rpx;
  border-radius: 28rpx;
  background: var(--primary);
  color: #ffffff;
  font-size: 60rpx;
  font-weight: 700;
}

.brand-name {
  font-size: 44rpx;
  font-weight: 700;
  color: var(--text);
}

.brand-sub {
  margin-top: 8rpx;
  font-size: 26rpx;
  color: var(--text-weak);
}

.form {
  padding: 32rpx 28rpx 28rpx;
}

.field {
  margin-bottom: 28rpx;
}

.f-label {
  display: block;
  font-size: 25rpx;
  color: var(--text-sub);
  margin-bottom: 12rpx;
}

.f-input {
  height: 88rpx;
  line-height: 88rpx;
  padding: 0 24rpx;
  box-sizing: border-box;
  background: var(--bg-sub);
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
  font-size: 30rpx;
  color: var(--text);
}

.f-ph {
  color: var(--text-weak);
}

.tip {
  margin-top: 24rpx;
}

.btn-disabled {
  opacity: 0.6;
}

.ok {
  color: var(--success);
}

.bad {
  color: var(--danger);
}

/* 手机号绑定面板 */
.bind-box {
  margin-top: 28rpx;
  padding-top: 24rpx;
  border-top: 1rpx solid var(--border);
}
.bind-title {
  font-size: 30rpx;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8rpx;
}
.bind-tip {
  font-size: 24rpx;
  color: var(--text-weak);
  line-height: 1.5;
  margin-bottom: 20rpx;
}
.bind-or {
  text-align: center;
  font-size: 24rpx;
  color: var(--text-weak);
  margin: 20rpx 0 12rpx;
}
button.btn {
  border: none;
  line-height: 2.4;
}
button.btn::after {
  border: none;
}
</style>
