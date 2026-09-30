/**
 * 认证状态管理 —— 令牌与用户信息的读写集中在此。
 *
 * 后端认证模型：
 *   - 登录（POST /auth/login）签发签名令牌，载荷含 sub/role/scope/exp；
 *   - 业务接口从 Authorization: Bearer <token> 解析身份（服务端验签）；
 *   - 未登录 → 匿名 viewer（只读）；写操作需 operator / admin；
 *   - approver 只用于 Agent 人工审批，不具备工单写权限。
 *
 * 所以前端只需要「存令牌 + 请求时带上」，角色判断用登录响应里返回的
 * role（仅用于 UI 显隐），真正的权限裁决在后端，前端显隐只是体验优化。
 */

const TOKEN_KEY = 'seasight_token'
const USER_KEY = 'seasight_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user || {}))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/**
 * 解码 JWT 载荷（不验签——验签是后端的事，前端只读 exp 做本地显隐）。
 * 手写 base64url：浏览器 atob 不认 -/_ 且无 padding，要先还原成标准 base64。
 * 任何一步失败都当"读不出有效期"返回 null，绝不抛错打断登录判断。
 */
function decodePayload(token) {
  try {
    const part = String(token).split('.')[1]
    if (!part) return null
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

/**
 * 令牌是否已过期。读不出 exp（格式异常/无 exp 字段）时按"未过期"处理——
 * 宁可让后端 401 来兜底，也不在前端误删一个其实还能用的令牌。
 */
export function isTokenExpired(token = getToken()) {
  if (!token) return true
  const payload = decodePayload(token)
  if (!payload || typeof payload.exp !== 'number') return false
  // exp 是秒级 Unix 时间；留 5s 余量，避免边界时刻前后端判定打架
  return payload.exp * 1000 <= Date.now() + 5000
}

export function isLoggedIn() {
  const token = getToken()
  if (!token) return false
  // 过期令牌留着只会让每次请求都 401；直接按未登录对待
  return !isTokenExpired(token)
}

/** 是否有写权限（仅前端显隐用，后端才是最终裁决） */
export function canWrite() {
  const u = getUser()
  return !!u && (u.role === 'admin' || u.role === 'operator')
}

export function roleLabel(role) {
  return {
    admin: '系统管理员',
    operator: '乡镇操作员',
    approver: '审批员',
    viewer: '访客',
  }[role] || role || '访客'
}
