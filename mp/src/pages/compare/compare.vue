<template>
  <view class="page" :class="'theme-' + state.theme">
    <page-header />

    <!-- 录入区 -->
    <view class="card">
      <view class="card-title">
        <text>实际销量录入</text>
        <text class="sub">{{ state.currentDate }}</text>
      </view>

      <view v-if="inputLoading" class="empty"><text class="e-icon">⏳</text>加载菜品中…</view>

      <view v-else-if="inputList.length === 0" class="empty">
        <text class="e-icon">🍽️</text>
        该日无备菜清单，请先在「概览」执行预测
      </view>

      <block v-else>
        <view class="tbl-head">
          <text class="c-name">菜品</text>
          <text class="c-pred">预测</text>
          <text class="c-act">实际销量</text>
          <text class="c-waste">浪费</text>
        </view>
        <view class="tbl-row" v-for="row in inputList" :key="row.dish_id">
          <text class="c-name">{{ row.name }}</text>
          <text class="c-pred">{{ row.predict != null ? num(row.predict) : '—' }}</text>
          <input
            class="c-act inp"
            type="digit"
            :value="inputs[row.dish_id] ? inputs[row.dish_id].actual : ''"
            :placeholder="row.predict != null ? String(row.predict) : '0'"
            @input="onAct(row.dish_id, $event.detail.value)"
          />
          <input
            class="c-waste inp"
            type="digit"
            :value="inputs[row.dish_id] ? inputs[row.dish_id].waste : ''"
            placeholder="0"
            @input="onWaste(row.dish_id, $event.detail.value)"
          />
        </view>

        <button class="btn-save" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存实际销量' }}</button>
        <view v-if="saveMsg" class="save-msg" :class="saveOk ? 'ok' : 'err'">{{ saveMsg }}</view>
      </block>
    </view>

    <!-- 对比区 -->
    <view class="card">
      <view class="card-title">
        <text>预测 vs 实际</text>
        <text class="sub">{{ state.currentDate }}</text>
      </view>

      <view v-if="cmpLoading" class="empty"><text class="e-icon">⏳</text>加载对比中…</view>

      <view v-else-if="cmpError" class="empty">
        <text class="e-icon">📊</text>{{ cmpError }}
      </view>

      <block v-else>
        <!-- 汇总 -->
        <view class="summary">
          <view class="s-item">
            <text class="s-num">{{ recordedCount }}/{{ cmpRows.length }}</text>
            <text class="s-lab">已录 / 总菜品</text>
          </view>
          <view class="s-item">
            <text class="s-num" :class="mape > 15 ? 'warn' : 'good'">{{ mape != null ? pct(mape) : '—' }}</text>
            <text class="s-lab">MAPE（平均偏差幅度）</text>
          </view>
          <view class="s-item">
            <text class="s-num" :class="avgBias > 0 ? 'over' : (avgBias < 0 ? 'under' : '')">{{ avgBias != null ? (avgBias > 0 ? '+' : '') + avgBias + '%' : '—' }}</text>
            <text class="s-lab">平均偏差方向</text>
          </view>
        </view>

        <view v-if="cmpRows.length === 0" class="empty">
          <text class="e-icon">📋</text>该日无预测菜品
        </view>

        <block v-else>
          <view class="cmp-row" v-for="(r, i) in cmpRows" :key="i">
            <view class="cmp-top">
              <text class="cmp-name">{{ r.dish }}</text>
              <text class="cmp-tag" :class="tagClass(r.status)">{{ tagText(r.status) }}</text>
              <text v-if="r.actual != null" class="cmp-src" :class="r.source === 'pos' ? 'src-pos' : 'src-manual'">{{ r.source === 'pos' ? 'POS自动' : '手工' }}</text>
            </view>
            <view class="cmp-bar">
              <view class="bar-line">
                <text class="bar-lab">预测</text>
                <view class="bar-track">
                  <view class="bar-fill pred" :style="{ width: barW(r.predict) }"></view>
                </view>
                <text class="bar-val">{{ num(r.predict) }}</text>
              </view>
              <view class="bar-line">
                <text class="bar-lab">实际</text>
                <view class="bar-track">
                  <view class="bar-fill act" :style="{ width: barW(r.actual) }"></view>
                </view>
                <text class="bar-val">{{ r.actual != null ? num(r.actual) : '—' }}</text>
              </view>
            </view>
            <view v-if="r.bias_pct != null" class="cmp-bias">
              偏差 {{ r.bias_pct > 0 ? '+' : '' }}{{ r.bias_pct }}%
            </view>
          </view>
        </block>
      </block>
    </view>
  </view>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { state, applyTabBar } from '../../store'
import { useAuthGuard } from '../../use/auth'
import { api } from '../../api'
import { num, pct } from '../../utils/format'
import pageHeader from '../../components/page-header.vue'

useAuthGuard()

const inputLoading = ref(false)
const inputList = ref([]) // [{dish_id, name, predict}]
const inputs = reactive({}) // dish_id -> {actual, waste}
const saving = ref(false)
const saveMsg = ref('')
const saveOk = ref(false)

const cmpLoading = ref(false)
const cmpError = ref('')
const cmpRows = ref([])

// ---- 录入清单：优先当日备菜（带预测默认），无预测则回退全量菜单 ----
async function loadInput() {
  if (state.currentStoreId == null) {
    inputList.value = []
    return
  }
  inputLoading.value = true
  try {
    let list = []
    try {
      const dishes = await api.dishes(state.currentStoreId, state.currentDate) // /api/prediction/{date}/dishes
      list = (dishes || []).map((d) => ({ dish_id: d.dish_id, name: d.dish, predict: d.predict_qty }))
    } catch (e) {
      // 该日无预测：回退全量菜单（无预测默认）
      const menu = await api.dishesList(state.currentStoreId)
      list = (menu.dishes || []).map((d) => ({ dish_id: d.id, name: d.name, predict: null }))
    }
    inputList.value = list
    // 初始化默认值：带入预测值（少打字）
    list.forEach((r) => {
      if (!inputs[r.dish_id]) {
        inputs[r.dish_id] = {
          actual: r.predict != null ? String(r.predict) : '',
          waste: '',
        }
      }
    })
  } catch (e) {
    inputList.value = []
  } finally {
    inputLoading.value = false
  }
}

function onAct(id, v) {
  if (!inputs[id]) inputs[id] = { actual: '', waste: '' }
  inputs[id].actual = v
}
function onWaste(id, v) {
  if (!inputs[id]) inputs[id] = { actual: '', waste: '' }
  inputs[id].waste = v
}

async function save() {
  if (state.currentStoreId == null) return
  const items = inputList.value
    .filter((r) => r.dish_id != null && inputs[r.dish_id] && inputs[r.dish_id].actual !== '' && inputs[r.dish_id].actual != null)
    .map((r) => ({
      dish_id: r.dish_id,
      actual_qty: Number(inputs[r.dish_id].actual) || 0,
      actual_waste: Number(inputs[r.dish_id].waste) || 0,
    }))
  if (items.length === 0) {
    saveMsg.value = '请至少填写一道菜品的实际销量'
    saveOk.value = false
    return
  }
  saving.value = true
  saveMsg.value = ''
  try {
    const res = await api.saveActualSales(state.currentStoreId, state.currentDate, items)
    saveOk.value = true
    saveMsg.value = `已保存 ${res.count} 道菜品的实际销量`
    await loadCompare() // 刷新对比
  } catch (e) {
    saveOk.value = false
    saveMsg.value = (e && e.message) || '保存失败，请重试'
  } finally {
    saving.value = false
  }
}

// ---- 对比 ----
async function loadCompare() {
  if (state.currentStoreId == null) {
    cmpRows.value = []
    cmpError.value = ''
    return
  }
  cmpLoading.value = true
  cmpError.value = ''
  try {
    const res = await api.compare(state.currentStoreId, state.currentDate)
    cmpRows.value = (res && res.rows) || []
  } catch (e) {
    if (e && e.status === 404) {
      cmpError.value = '该日暂无预测结果，请先在「概览」执行预测'
    } else {
      cmpError.value = '加载对比失败'
    }
    cmpRows.value = []
  } finally {
    cmpLoading.value = false
  }
}

const recordedCount = computed(() => cmpRows.value.filter((r) => r.actual != null).length)
const mape = computed(() => {
  const valid = cmpRows.value.filter((r) => r.actual != null && r.bias_pct != null)
  if (!valid.length) return null
  const sum = valid.reduce((s, r) => s + Math.abs(r.bias_pct), 0)
  return Math.round((sum / valid.length) * 10) / 10
})
const avgBias = computed(() => {
  const valid = cmpRows.value.filter((r) => r.actual != null && r.bias_pct != null)
  if (!valid.length) return null
  const sum = valid.reduce((s, r) => s + r.bias_pct, 0)
  return Math.round((sum / valid.length) * 10) / 10
})

function tagClass(status) {
  if (status === 'over') return 't-over'
  if (status === 'under') return 't-under'
  if (status === 'match') return 't-match'
  return 't-none'
}
function tagText(status) {
  if (status === 'over') return '偏高'
  if (status === 'under') return '偏低'
  if (status === 'match') return '持平'
  return '未录'
}

const maxVal = computed(() => {
  let m = 0
  cmpRows.value.forEach((r) => {
    m = Math.max(m, Number(r.predict) || 0, Number(r.actual) || 0)
  })
  return m || 1
})
function barW(v) {
  const n = Number(v) || 0
  return Math.max(2, Math.round((n / maxVal.value) * 100)) + '%'
}

// 门店 / 日期变化即刷新
watch(() => state.currentStoreId, () => {
  loadInput()
  loadCompare()
})
watch(() => state.currentDate, () => {
  loadInput()
  loadCompare()
})

onShow(() => {
  applyTabBar(state.theme)
  loadInput()
  loadCompare()
})
</script>

<style scoped>
.card {
  background: var(--bg-card);
  border: 1rpx solid var(--border);
  border-radius: 16rpx;
  padding: 24rpx;
  margin-bottom: 24rpx;
}
.card-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 20rpx;
}
.card-title > text:first-child {
  font-size: 32rpx;
  font-weight: 700;
  color: var(--text);
}
.sub {
  font-size: 24rpx;
  color: var(--text-weak);
  font-variant-numeric: tabular-nums;
}

.empty {
  text-align: center;
  padding: 48rpx 0;
  color: var(--text-weak);
  font-size: 26rpx;
}
.empty .e-icon {
  display: block;
  font-size: 56rpx;
  margin-bottom: 12rpx;
}

/* 录入表格 */
.tbl-head, .tbl-row {
  display: flex;
  align-items: center;
  padding: 14rpx 0;
}
.tbl-head {
  border-bottom: 1rpx solid var(--border);
  font-size: 23rpx;
  color: var(--text-weak);
}
.tbl-row {
  border-bottom: 1rpx solid var(--border);
}
.c-name { flex: 1; font-size: 27rpx; color: var(--text); }
.c-pred { width: 110rpx; text-align: right; font-size: 25rpx; color: var(--text-weak); font-variant-numeric: tabular-nums; }
.c-act { width: 150rpx; }
.c-waste { width: 110rpx; }
.inp {
  background: var(--bg-sub);
  border: 1rpx solid var(--border);
  border-radius: 8rpx;
  padding: 10rpx 14rpx;
  font-size: 26rpx;
  color: var(--text);
  text-align: right;
  margin-left: 12rpx;
}
.c-act.inp { text-align: right; }
.c-waste.inp { text-align: right; margin-left: 12rpx; }

/* 数据来源徽标（POS自动 / 手工） */
.cmp-src {
  font-size: 20rpx;
  padding: 2rpx 10rpx;
  border-radius: 8rpx;
  margin-left: 10rpx;
  line-height: 1.4;
}
.src-pos { background: #e6f4ea; color: #1e7e34; }
.src-manual { background: #f0f0f0; color: #888; }

.btn-save {
  margin-top: 24rpx;
  background: var(--primary);
  color: #fff;
  font-size: 29rpx;
  border-radius: 12rpx;
  padding: 18rpx 0;
}
.btn-save[disabled] { opacity: 0.6; }
.save-msg {
  margin-top: 16rpx;
  font-size: 25rpx;
  text-align: center;
}
.save-msg.ok { color: #2e9b5b; }
.save-msg.err { color: #e15554; }

/* 汇总 */
.summary {
  display: flex;
  margin-bottom: 20rpx;
}
.s-item {
  flex: 1;
  text-align: center;
  border-right: 1rpx solid var(--border);
}
.s-item:last-child { border-right: none; }
.s-num {
  display: block;
  font-size: 38rpx;
  font-weight: 800;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.s-num.good { color: #2e9b5b; }
.s-num.warn { color: #e15554; }
.s-num.over { color: #e15554; }
.s-num.under { color: #2e9b5b; }
.s-lab {
  font-size: 21rpx;
  color: var(--text-weak);
}

/* 对比行 */
.cmp-row {
  border-bottom: 1rpx solid var(--border);
  padding: 16rpx 0;
}
.cmp-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10rpx;
}
.cmp-name { font-size: 27rpx; color: var(--text); font-weight: 600; }
.cmp-tag {
  font-size: 22rpx;
  padding: 3rpx 14rpx;
  border-radius: 20rpx;
}
.t-over { background: rgba(225, 85, 84, 0.14); color: #e15554; }
.t-under { background: rgba(46, 155, 91, 0.14); color: #2e9b5b; }
.t-match { background: rgba(120, 120, 120, 0.14); color: #6b7684; }
.t-none { background: rgba(120, 120, 120, 0.1); color: #8a919f; }

.cmp-bar { padding: 4rpx 0; }
.bar-line { display: flex; align-items: center; margin: 5rpx 0; }
.bar-lab { width: 56rpx; font-size: 22rpx; color: var(--text-weak); }
.bar-track {
  flex: 1;
  height: 18rpx;
  background: var(--bg-sub);
  border-radius: 9rpx;
  overflow: hidden;
  margin: 0 12rpx;
}
.bar-fill { height: 100%; border-radius: 9rpx; }
.bar-fill.pred { background: #9aa6b2; }
.bar-fill.act { background: var(--primary); }
.bar-val {
  width: 90rpx;
  text-align: right;
  font-size: 23rpx;
  color: var(--text-sub);
  font-variant-numeric: tabular-nums;
}
.cmp-bias {
  font-size: 22rpx;
  color: var(--text-weak);
  margin-top: 4rpx;
}
</style>
