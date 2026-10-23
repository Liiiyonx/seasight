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
 * 解码令牌载荷（不验签——验签是后端的事，前端只读 role/exp 做本地显隐）。
 * 手写 base64url：浏览器 atob 不认 -/_ 且无 padding，要先还原成标准 base64。
 * 任何一步失败都当"读不出"返回 null，绝不抛错打断登录判断。
 *
 * 段数兼容两种格式：
 *   - 本后端令牌是 2 段式 payload.signature（后端用 rsplit('.', 1) 验签，见 auth.py）；
 *   - 标准 JWT 是 3 段式 header.payload.signature。
 * 之前固定取 [1] 在本后端拿到的是签名 → 解析失败 → role 静默回退 localStorage，
 * 于是"令牌只读 + 本地 user 是 admin"时写按钮照常显示、一提交就 403。
 */
function decodePayload(token) {
  try {
    const parts = String(token).split('.')
    const part = parts.length >= 3 ? parts[1] : parts[0]
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

/**
 * 当前角色：**以令牌载荷为准**（与后端裁决同源）。
 * 之前 UI 用 localStorage 里的 user.role，一旦令牌与用户对象不一致
 * （换账号、令牌重签、手工改库），就会出现"按钮显示可写、一提交就 403"
 * 的假闭环 —— 角色判断必须和令牌绑定。
 */
export function currentRole() {
  const payload = decodePayload(getToken())
  if (payload && typeof payload.role === 'string' && payload.role) return payload.role
  return getUser()?.role || 'viewer'
}

/** 是否有写权限（仅前端显隐用，后端才是最终裁决） */
export function canWrite() {
  const role = currentRole()
  return role === 'admin' || role === 'operator'
}

export function roleLabel(role) {
  return {
    admin: '系统管理员',
    operator: '乡镇操作员',
    approver: '审批员',
    viewer: '访客',
  }[role] || role || '访客'
}
