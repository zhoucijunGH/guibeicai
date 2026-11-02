/**
 * 数字格式化工具（无 Intl/locale 依赖，H5 与原生平台通用）
 */

function groupThousands(s) {
  const neg = s.startsWith('-')
  const digits = neg ? s.slice(1) : s
  const [intp, dec] = digits.split('.')
  const withSep = (intp || '0').replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return (neg ? '-' : '') + withSep + (dec ? '.' + dec : '')
}

/** 金额：¥1,234.56 */
export function money(n) {
  const v = Number(n || 0)
  return '¥' + groupThousands(v.toFixed(2))
}

/** 纯数字（可带小数位）：1,234 */
export function num(n, d = 0) {
  const v = Number(n || 0)
  return groupThousands(v.toFixed(d))
}

/** 百分比：80.22% */
export function pct(n) {
  return Number(n || 0).toFixed(2) + '%'
}

/** 风险等级 → 颜色类（tag-xxx） */
export function riskTag(risk) {
  if (risk === '高') return 'tag-danger'
  if (risk === '偏高') return 'tag-warn'
  return 'tag-success'
}
