<template>
  <view class="page" :class="'theme-' + state.theme">
    <page-header />

    <view class="card">
      <view class="card-title">
        <text>单菜品维度 · {{ state.currentDate }}</text>
        <text class="sub" @click="refresh">刷新</text>
      </view>

      <view v-if="loading" class="empty"><text class="e-icon">⏳</text>加载中…</view>

      <view v-else-if="dishes.length" class="hint">
        共 <text class="strong">{{ dishes.length }}</text> 个菜品，点击展开查看食材采购需求。
        <text class="muted">（每份用量 × 预测销量 = 当日需求）</text>
      </view>
      <view v-else class="empty">
        <text class="e-icon">🍲</text>
        {{ errMsg || '该日暂无预测结果' }}
      </view>

      <view v-for="d in dishes" :key="d.dish" class="dish">
        <view class="dish-head" @click="toggle(d.dish)">
          <view class="dish-name">{{ d.dish }}</view>
          <view class="dish-qty">
            预测 <text class="strong">{{ num(d.predict_qty, 1) }}</text> 份
            <text class="dish-arrow" :class="{ open: expanded[d.dish] }">▾</text>
          </view>
        </view>

        <view v-if="expanded[d.dish]" class="dish-body">
          <view v-if="!d.ingredients.length" class="ing-empty">该菜品暂无配方（食材）数据</view>
          <view v-for="(ing, i) in d.ingredients" :key="i" class="ing-row">
            <text class="ing-name">{{ ing.ingredient }}</text>
            <text class="ing-unit">{{ num(ing.quantity, 2) }} {{ ing.unit }}/份</text>
            <text class="ing-price">单价 {{ money(ing.unit_price) }}</text>
            <text class="ing-amt">每份 {{ money(ingSubtotal(ing)) }}</text>
          </view>
          <view class="ing-sum">
            当日食材成本合计 <text class="strong">{{ money(dailyCost(d)) }}</text>
            · 共 {{ d.ingredients.length }} 种食材
          </view>
        </view>
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
const dishes = ref([])
const errMsg = ref('')
const expanded = reactive({})

function toggle(name) {
  expanded[name] = !expanded[name]
}

// 每份小计 = 用量 × 单价（接口未返回 amount，端上计算）
function ingSubtotal(ing) {
  return Number(ing.quantity || 0) * Number(ing.unit_price || 0)
}
// 当日成本 = 预测销量 × Σ(每份用量 × 单价)
function dailyCost(d) {
  const per = d.ingredients.reduce((s, x) => s + ingSubtotal(x), 0)
  return d.predict_qty * per
}

async function load() {
  if (state.currentStoreId == null) {
    errMsg.value = '请先选择门店'
    dishes.value = []
    return
  }
  loading.value = true
  errMsg.value = ''
  try {
    const res = await api.dishes(state.currentStoreId, state.currentDate)
    dishes.value = Array.isArray(res) ? res : []
  } catch (e) {
    dishes.value = []
    errMsg.value = e && e.code === 404 ? '该日暂无预测结果，请先在「概览」执行预测' : errText(e, '加载失败')
  } finally {
    loading.value = false
  }
}

function refresh() {
  load()
}

watch(() => [state.currentStoreId, state.currentDate], () => load())

onShow(() => {
  applyTabBar(state.theme)
  load()
})
</script>

<style scoped>
.dish {
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
  margin-bottom: 18rpx;
  overflow: hidden;
}
.dish-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 24rpx;
  background: var(--bg-sub);
}
.dish-name {
  font-size: 29rpx;
  font-weight: 600;
  color: var(--text);
}
.dish-qty {
  font-size: 25rpx;
  color: var(--text-sub);
}
.dish-arrow {
  margin-left: 8rpx;
  transition: transform 0.2s;
  display: inline-block;
}
.dish-arrow.open {
  transform: rotate(180deg);
}
.dish-body {
  padding: 8rpx 24rpx 20rpx;
}
.ing-empty {
  padding: 20rpx 0;
  font-size: 25rpx;
  color: var(--text-weak);
}
.ing-row {
  display: flex;
  align-items: center;
  padding: 14rpx 0;
  border-bottom: 1rpx solid var(--border);
  font-size: 25rpx;
  color: var(--text);
}
.ing-name {
  flex: 2.4;
}
.ing-unit {
  flex: 2;
  color: var(--text-sub);
}
.ing-price {
  flex: 1.4;
  text-align: right;
  color: var(--text-sub);
}
.ing-amt {
  flex: 1.4;
  text-align: right;
  color: var(--primary);
}
.ing-sum {
  margin-top: 14rpx;
  font-size: 25rpx;
  color: var(--text-sub);
  text-align: right;
}
</style>
