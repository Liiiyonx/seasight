<template>
  <!--
    ★ accounts 为空（生产构建未注入 VITE_DEMO_ACCOUNTS）时整个面板不渲染。
      演示前要它出现：设 VITE_DEMO_ACCOUNTS 后重新 npm run build。
-->
  <section v-if="accounts.length" class="login-demo" aria-labelledby="demo-account-title">
    <div class="login-demo__head">
      <div>
        <h3 id="demo-account-title" class="login-demo__title">测试账号</h3>
        <p class="login-demo__sub">点击任意账号，自动填入登录信息</p>
      </div>
      <span class="login-demo__tag">DEMO</span>
    </div>

    <div class="login-demo__list">
      <button
        v-for="account in accounts"
        :key="account.username"
        class="login-demo__item"
        :class="{ 'login-demo__item--filled': filled === account.username }"
        type="button"
        :aria-label="`填入${account.label}账号 ${account.username}`"
        @click="$emit('fill', account)"
      >
        <span class="login-demo__avatar" :class="`login-demo__avatar--${account.tone}`">
          {{ account.short }}
        </span>
        <span class="login-demo__body">
          <span class="login-demo__role">{{ account.label }}</span>
          <span class="login-demo__credentials">
            <code>{{ account.username }}</code>
            <span class="login-demo__slash">/</span>
            <!--
              ★ 口令默认打码。原先明文显示，会同时泄漏到三处：
              截图（→ 软著材料 / 答辩PPT）、投屏、公开仓库。
              需要现场演示明文时设 VITE_SHOW_DEMO_PASSWORD=1 重新构建。
              无论开关如何，aria-label 都不带口令 —— 读屏会念出来，
              等于把口令念给全场听。
            -->
            <code v-if="showPassword">{{ account.password }}</code>
            <code v-else aria-hidden="true">{{ masked }}</code>
          </span>
        </span>
        <span class="login-demo__action">
          {{ filled === account.username ? '已填入' : '填入' }}
        </span>
      </button>
    </div>
  </section>
</template>

<script setup>
defineProps({
  filled: { type: String, default: '' },
})

defineEmits(['fill'])

/**
 * 是否明文显示演示口令。
 * 默认 false —— 演示时点一下就能填入，并不需要看见口令。
 */
const showPassword = import.meta.env.VITE_SHOW_DEMO_PASSWORD === '1'

/** 打码占位：固定长度，避免从位数泄露口令长度 */
const masked = '••••••••'

/**
 * 演示账号表。
 *
 * ★ 口令**不在源码里**，改由构建期环境变量注入：
 *     VITE_DEMO_ACCOUNTS='[{"username":"admin","password":"...","label":"系统管理员","short":"管","tone":"blue"}]'
 *   未配置时 accounts 为空数组，面板整体不渲染 ——
 *   这样生产构建的产物里**不会残留任何明文口令**。
 *
 * 为什么这样改：原先口令硬编码在源码里，会同时泄漏到
 *   ①打包产物 → 公开仓库
 *   ②登录页截图 → 软著材料 / 答辩 PPT
 *   ③aria-label → 读屏软件把口令念给全场听
 * 三条路都堵住，才算真解决。
 */
const accounts = parseDemoAccounts(import.meta.env.VITE_DEMO_ACCOUNTS)

/** 解析 env 里的 JSON 账号表；任何异常都退化为空，绝不让构建失败 */
function parseDemoAccounts(raw) {
  if (!raw) return []
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return []
    return arr
      .filter((a) => a && a.username)
      .map((a) => ({
        username: String(a.username),
        password: String(a.password || ''),
        label: String(a.label || a.username),
        short: String(a.short || a.username.slice(0, 1)),
        tone: ['blue', 'green', 'orange', 'gray'].includes(a.tone)
          ? a.tone : 'blue',
      }))
  } catch {
    return []
  }
}
</script>

<style scoped>
.login-demo {
  margin-top: 24px;
  padding-top: 22px;
  border-top: 1px solid var(--separator);
}

.login-demo__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.login-demo__title {
  color: var(--text-main);
  font-size: 14px;
  font-weight: 650;
  letter-spacing: 0;
}

.login-demo__sub {
  margin-top: 2px;
  color: var(--text-dim);
  font-size: 12px;
}

.login-demo__tag {
  flex: 0 0 auto;
  padding: 3px 7px;
  border-radius: 999px;
  background: var(--bg-active);
  color: var(--c-primary);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
}

.login-demo__list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.login-demo__item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 58px;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 12px;
  background: var(--bg-panel-2);
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
  transition:
    background var(--dur) var(--ease),
    border-color var(--dur) var(--ease),
    transform 0.12s var(--ease);
}

.login-demo__item:hover {
  border-color: color-mix(in srgb, var(--c-primary) 26%, transparent);
  background: var(--bg-hover);
}

.login-demo__item:active {
  transform: scale(0.99);
}

.login-demo__item--filled {
  border-color: color-mix(in srgb, var(--c-primary) 38%, transparent);
  background: var(--bg-active);
}

.login-demo__avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  color: #ffffff;
  font-size: 13px;
  font-weight: 700;
}

.login-demo__avatar--blue { background: #007aff; }
.login-demo__avatar--green { background: #34c759; }
.login-demo__avatar--orange { background: #ff9500; }
.login-demo__avatar--gray { background: #8e8e93; }

.login-demo__body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 3px;
}

.login-demo__role {
  color: var(--text-main);
  font-size: 12.5px;
  font-weight: 550;
  line-height: 1.25;
}

.login-demo__credentials {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  min-width: 0;
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.35;
}

.login-demo__credentials code {
  color: var(--text-sub);
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
  overflow-wrap: anywhere;
}

.login-demo__slash {
  color: var(--text-dim);
}

.login-demo__action {
  flex: 0 0 auto;
  min-width: 40px;
  padding: 4px 7px;
  border-radius: 8px;
  background: var(--bg-panel);
  color: var(--c-primary);
  font-size: 11px;
  font-weight: 600;
  text-align: center;
  white-space: nowrap;
}

.login-demo__item--filled .login-demo__action {
  background: var(--c-primary);
  color: #ffffff;
}

@media (max-width: 560px) {
  .login-demo {
    margin-top: 20px;
    padding-top: 18px;
  }

  .login-demo__item {
    grid-template-columns: 32px minmax(0, 1fr) auto;
    gap: 9px;
    padding: 8px 9px;
  }

  .login-demo__avatar {
    width: 32px;
    height: 32px;
    border-radius: 9px;
  }

  .login-demo__action {
    min-width: 37px;
    padding-inline: 6px;
  }
}
</style>
