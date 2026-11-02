<template>
  <view class="page" :class="'theme-' + state.theme">
    <page-header />

    <view class="card">
      <view class="card-title">
        <text>采购单推送 · {{ state.currentDate }}</text>
        <text class="sub" @click="load">刷新</text>
      </view>

      <view class="actions">
        <view class="act-btn act-sub" @click="subscribe">
          <text class="act-ico">🔔</text> 订阅采购单推送
        </view>
        <view class="act-btn act-push" @click="pushToMe">
          <text class="act-ico">📲</text> 立即推送给我
        </view>
      </view>
      <view class="tip">订阅后，每晚预测生成采购单会自动发到你的微信；「立即推送」可手动把当前采购单发到你的微信。</view>

      <view v-if="pushing" class="empty"><text class="e-icon">⏳</text>推送中…</view>

      <view v-else-if="pushResult" class="push-result" :class="pushOk ? 'ok' : 'bad'">
        推送结果：邮件 {{ pushResult.email ? '✅' : '—' }} ／ 企微 {{ pushResult.wecom ? '✅' : '—' }} ／ 微信 {{ pushResult.wx_subscribe ? '✅' : '—' }}
        <view class="pr-sub">微信未送达通常是未订阅模板或 AppID 未配置，属正常（详见下方提示）。</view>
      </view>

      <view v-if="loading" class="empty"><text class="e-icon">⏳</text>加载中…</view>

      <block v-else-if="summary">
        <view class="metrics">
          <view class="metric">
            <view class="m-label">采购总额</view>
            <view class="m-value">{{ money(summary.total_cost) }}</view>
          </view>
          <view class="metric">
            <view class="m-label">采购总重</view>
            <view class="m-value">{{ num(summary.total_qty, 1) }}</view>
          </view>
          <view class="metric">
            <view class="m-label">涉及菜品</view>
            <view class="m-value">{{ summary.dish_count }}</view>
          </view>
          <view class="metric">
            <view class="m-label">食材种类</view>
            <view class="m-value">{{ items.length }}</view>
          </view>
        </view>

        <view v-if="!items.length" class="empty"><text class="e-icon">🧺</text>暂无食材数据</view>

        <view v-for="(it, i) in items" :key="i" class="ing">
          <view class="ing-head">
            <view class="ing-left">
              <text class="ing-name">{{ it.ingredient_name }}</text>
              <text class="tag tag-info ing-cat">{{ it.category || '其他' }}</text>
            </view>
            <view class="ing-right">
              <text class="ing-amt">{{ money(it.cost) }}</text>
              <text class="ing-qty">{{ num(it.quantity, 2) }} {{ it.unit }}</text>
            </view>
          </view>
        </view>
      </block>

      <view v-else class="empty">
        <text class="e-icon">🧺</text>
        {{ errMsg || '该日暂无预测结果' }}
      </view>
    </view>
  </view>
</template>

<script setup>
import { ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { state, applyTabBar } from '../../store'
import { api } from '../../api'
import { useAuthGuard, errText } from '../../use/auth'
import pageHeader from '../../components/page-header.vue'
import { money, num } from '../../utils/format'
import { WX_SUBSCRIBE_TMPL_ID } from '../../config'

useAuthGuard()

const loading = ref(false)
const pushing = ref(false)
const items = ref([])
const summary = ref(null)
const errMsg = ref('')
const pushResult = ref(null)
const pushOk = ref(false)

async function load() {
  if (state.currentStoreId == null) {
    errMsg.value = '请先选择门店'
    items.value = []
    summary.value = null
    return
  }
  loading.value = true
  errMsg.value = ''
  try {
    const res = await api.ingredients(state.currentStoreId, state.currentDate)
    items.value = (res && res.items) || []
    summary.value = {
      total_cost: res.total_cost,
      total_qty: res.total_qty,
      dish_count: res.dish_count,
    }
  } catch (e) {
    items.value = []
    summary.value = null
    errMsg.value = e && e.code === 404 ? '该日暂无预测结果，请先在「概览」执行预测' : errText(e, '加载失败')
  } finally {
    loading.value = false
  }
}

// 微信订阅消息：请求用户授权接收采购单推送（需后端配置模板 ID）
function subscribe() {
  if (!WX_SUBSCRIBE_TMPL_ID) {
    uni.showToast({ title: '未配置订阅模板，暂不可订阅', icon: 'none' })
    return
  }
  if (typeof uni.requestSubscribeMessage !== 'function') {
    uni.showToast({ title: '当前环境不支持订阅（请用微信小程序）', icon: 'none' })
    return
  }
  uni.requestSubscribeMessage({
    tmplIds: [WX_SUBSCRIBE_TMPL_ID],
    success: () => uni.showToast({ title: '已订阅，每晚自动推送', icon: 'success' }),
    fail: () => uni.showToast({ title: '订阅取消或未授权', icon: 'none' }),
  })
}

// 立即把当前采购单推送到我的微信（及门店配置的邮件/企微）
async function pushToMe() {
  if (state.currentStoreId == null) {
    uni.showToast({ title: '请先选择门店', icon: 'none' })
    return
  }
  pushing.value = true
  pushResult.value = null
  try {
    const res = await api.pushPurchaseOrder(state.currentStoreId, state.currentDate)
    pushResult.value = res.channels || {}
    pushOk.value = true
    uni.showToast({ title: '已发起推送', icon: 'success' })
  } catch (e) {
    pushOk.value = false
    pushResult.value = { email: false, wecom: false, wx_subscribe: false }
    uni.showToast({ title: errText(e, '推送失败'), icon: 'none' })
  } finally {
    pushing.value = false
  }
}

onShow(() => {
  applyTabBar(state.theme)
  load()
})
</script>

<style scoped>
.actions {
  display: flex;
  gap: 16rpx;
  margin-bottom: 12rpx;
}
.act-btn {
  flex: 1;
  height: 80rpx;
  line-height: 80rpx;
  text-align: center;
  border-radius: 12rpx;
  font-size: 27rpx;
  color: #fff;
}
.act-sub {
  background: var(--primary);
}
.act-push {
  background: #07c160;
}
.act-ico {
  margin-right: 8rpx;
}
.tip {
  font-size: 22rpx;
  color: var(--text-weak);
  margin-bottom: 16rpx;
  line-height: 1.5;
}
.push-result {
  font-size: 24rpx;
  padding: 16rpx 20rpx;
  border-radius: 12rpx;
  margin-bottom: 16rpx;
}
.push-result.ok {
  background: rgba(7, 193, 96, 0.1);
  color: #07c160;
}
.push-result.bad {
  background: rgba(225, 85, 84, 0.1);
  color: var(--danger);
}
.pr-sub {
  margin-top: 8rpx;
  font-size: 21rpx;
  opacity: 0.8;
}
.metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 12rpx;
  margin-bottom: 16rpx;
}
.metric {
  flex: 1 1 44%;
  background: var(--bg-sub);
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
  padding: 18rpx 20rpx;
}
.m-label {
  font-size: 22rpx;
  color: var(--text-weak);
}
.m-value {
  font-size: 32rpx;
  font-weight: 700;
  color: var(--text);
  margin-top: 4rpx;
}
.ing {
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
  margin-bottom: 16rpx;
  overflow: hidden;
}
.ing-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 22rpx 24rpx;
  background: var(--bg-sub);
}
.ing-left {
  display: flex;
  align-items: center;
  flex: 1;
  min-width: 0;
}
.ing-name {
  font-size: 28rpx;
  font-weight: 600;
  color: var(--text);
  margin-right: 12rpx;
}
.ing-cat {
  font-size: 20rpx;
}
.ing-right {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  margin-left: 12rpx;
}
.ing-amt {
  font-size: 28rpx;
  font-weight: 700;
  color: var(--primary);
  margin-right: 16rpx;
}
.ing-qty {
  font-size: 23rpx;
  color: var(--text-sub);
}
.empty {
  text-align: center;
  color: var(--text-weak);
  padding: 60rpx 0;
  font-size: 26rpx;
}
.e-icon {
  display: block;
  font-size: 48rpx;
  margin-bottom: 12rpx;
}
</style>
