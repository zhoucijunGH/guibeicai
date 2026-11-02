<template>
  <view class="page" :class="'theme-' + state.theme">
    <page-header />

    <view class="card">
      <view class="card-title">
        <text>食材采购汇总 · {{ state.currentDate }}</text>
        <text class="sub" @click="goNotice">📲 推送</text>
        <text class="sub" @click="refresh">刷新</text>
      </view>

      <view v-if="loading" class="empty"><text class="e-icon">⏳</text>加载中…</view>

      <block v-else-if="summary">
        <!-- 顶部汇总 -->
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
          <view class="ing-head" @click="toggle(i)">
            <view class="ing-left">
              <text class="ing-name">{{ it.ingredient_name }}</text>
              <text class="tag tag-info ing-cat">{{ it.category || '其他' }}</text>
            </view>
            <view class="ing-right">
              <text class="ing-amt">{{ money(it.cost) }}</text>
              <text class="ing-qty">{{ num(it.quantity, 2) }} {{ it.unit }}</text>
              <text class="ing-arrow" :class="{ open: expanded[i] }">▾</text>
            </view>
          </view>

          <view v-if="expanded[i]" class="ing-body">
            <view class="bk-title">来源拆解（共 {{ (it.breakdown || []).length }} 个菜品）</view>
            <view v-for="(b, j) in it.breakdown" :key="j" class="bk-row">
              <text class="bk-dish">{{ b.dish_name }}</text>
              <text class="bk-qty">{{ num(b.quantity, 2) }} {{ it.unit }}</text>
              <text class="bk-amt">{{ money(b.cost) }}</text>
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
import { ref, reactive, watch } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { state, applyTabBar } from '../../store'
import { api } from '../../api'
import { useAuthGuard, errText } from '../../use/auth'
import pageHeader from '../../components/page-header.vue'
import { money, num } from '../../utils/format'

useAuthGuard()

const loading = ref(false)
const items = ref([])
const summary = ref(null)
const errMsg = ref('')
const expanded = reactive({})

function toggle(i) {
  expanded[i] = !expanded[i]
}

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

function refresh() {
  load()
}

function goNotice() {
  uni.navigateTo({ url: '/pages/notice/notice' })
}

watch(() => [state.currentStoreId, state.currentDate], () => load())

onShow(() => {
  applyTabBar(state.theme)
  load()
})
</script>

<style scoped>
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
  margin-left: 0;
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
  margin-right: 12rpx;
}
.ing-arrow {
  font-size: 22rpx;
  color: var(--text-weak);
  transition: transform 0.2s;
  display: inline-block;
}
.ing-arrow.open {
  transform: rotate(180deg);
}
.ing-body {
  padding: 8rpx 24rpx 20rpx;
}
.bk-title {
  font-size: 23rpx;
  color: var(--text-weak);
  padding: 12rpx 0;
}
.bk-row {
  display: flex;
  align-items: center;
  padding: 12rpx 0;
  border-bottom: 1rpx solid var(--border);
  font-size: 25rpx;
  color: var(--text);
}
.bk-dish {
  flex: 2;
}
.bk-qty {
  flex: 1.6;
  text-align: right;
  color: var(--text-sub);
}
.bk-amt {
  flex: 1.4;
  text-align: right;
  color: var(--primary);
}
</style>
