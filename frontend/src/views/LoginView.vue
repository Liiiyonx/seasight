<template>
  <div class="login">
    <section
      class="login__story"
      :style="{ '--login-coast-image': `url(${loginCoastImage})` }"
    >
      <div class="login__story-inner">
        <BrandLockup
          variant="hero"
          :size="52"
          subtitle="SeaSight · 海洋漂浮垃圾智能治理平台"
        />

        <div class="login__intro">
          <span class="login__eyebrow">DATA TO DECISION</span>
          <h1>让每一次发现，<br />都有据可循。</h1>
          <p>连接多模态数据、领域知识与治理行动，形成可追溯的智能决策闭环。</p>
        </div>

        <div class="login__story-meta">
          <div class="login__story-foot">
            <span class="login__pulse"></span>
            <span>连江县 · 海洋垃圾智能治理</span>
          </div>
          <span class="login__photo-credit">4K 海岸实景 · 图片来源：Unsplash</span>
        </div>
      </div>
    </section>

    <section class="login__access">
      <div class="login__card">
        <header class="login__head">
          <span class="login__kicker">账号登录</span>
          <h2>欢迎回来</h2>
          <p>登录后进入 SeaSight 治理平台</p>
        </header>

        <form class="login__form" @submit.prevent="submit">
          <label class="login__field" for="username">
            <span>账号</span>
            <input
              id="username"
              v-model.trim="username"
              type="text"
              autocomplete="username"
              placeholder="请输入账号"
              @input="filledUsername = ''"
            />
          </label>

          <label class="login__field" for="password">
            <span>密码</span>
            <span class="login__password">
              <input
                id="password"
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="current-password"
                placeholder="请输入密码"
                @input="filledUsername = ''"
              />
              <button
                class="login__reveal"
                type="button"
                :aria-pressed="showPassword"
                :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                @click="showPassword = !showPassword"
              >
                {{ showPassword ? '隐藏' : '显示' }}
              </button>
            </span>
          </label>

          <p v-if="error" class="login__error" role="alert">{{ error }}</p>

          <button ref="submitButton" class="login__btn" type="submit" :disabled="loading">
            <span v-if="loading" class="login__spinner" aria-hidden="true"></span>
            <span>{{ loading ? '登录中' : '登录' }}</span>
          </button>
        </form>

        <LoginDemoPanel :filled="filledUsername" @fill="fillDemo" />
      </div>

      <p class="login__foot">SeaSight 智能治理平台 · 内部演示环境</p>
    </section>
  </div>
</template>

<script setup>
import { nextTick, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { authApi } from '@/api'
import { setAuth } from '@/utils/auth'
import BrandLockup from '@/components/BrandLockup.vue'
import LoginDemoPanel from '@/components/LoginDemoPanel.vue'
import loginCoastImage from '@/assets/coast-aerial-4k.webp'

const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')
const showPassword = ref(false)
const filledUsername = ref('')
const submitButton = ref(null)

async function submit() {
  if (!username.value || !password.value) {
    error.value = '请输入账号和密码'
    return
  }

  loading.value = true
  error.value = ''
  try {
    const res = await authApi.login(username.value, password.value)
    setAuth(res.access_token, {
      username: username.value,
      role: res.role,
      full_name: res.full_name,
      township_scope: res.township_scope,
    })
    router.replace(route.query.redirect || '/dashboard')
  } catch (err) {
    error.value = err.message || '登录失败'
  } finally {
    loading.value = false
  }
}

async function fillDemo(account) {
  username.value = account.username
  password.value = account.password
  error.value = ''
  filledUsername.value = account.username
  await nextTick()
  submitButton.value?.focus()
}
</script>

<style scoped>
.login {
  display: grid;
  grid-template-columns: minmax(430px, 0.92fr) minmax(520px, 1.08fr);
  height: 100vh;
  height: 100dvh;
  min-height: 100vh;
  min-height: 100dvh;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  scroll-padding-block: 20px 120px;
  background: var(--bg-page);
  -webkit-overflow-scrolling: touch;
}

/* 4K 海岸航拍叠加横向渐层，让高清照片自然过渡到右侧登录区。 */
.login__story {
  position: relative;
  display: flex;
  min-width: 0;
  overflow: hidden;
  isolation: isolate;
  background: #061a2f;
  color: #ffffff;
}

.login__story::before,
.login__story::after {
  content: '';
  position: absolute;
  inset: 0;
}

.login__story::before {
  z-index: -2;
  background-image: var(--login-coast-image);
  background-position: 48% 55%;
  background-size: cover;
  filter: saturate(1.06) contrast(1.04);
  transform: scale(1.008);
}

.login__story::after {
  z-index: -1;
  background:
    linear-gradient(
      90deg,
      rgba(3, 20, 34, 0.84) 0%,
      rgba(3, 24, 40, 0.52) 36%,
      rgba(3, 26, 43, 0.12) 57%,
      rgba(3, 23, 39, 0.38) 78%,
      var(--bg-page) 100%
    ),
    linear-gradient(
      180deg,
      rgba(2, 15, 28, 0.5) 0%,
      rgba(2, 17, 31, 0.16) 48%,
      rgba(2, 17, 31, 0.74) 100%
    ),
    radial-gradient(circle at 18% 12%, rgba(0, 188, 233, 0.16), transparent 38%);
}

.login__story-inner {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  width: min(100%, 640px);
  min-height: 100%;
  padding: clamp(42px, 6vw, 82px);
}

.login__story-inner > * {
  text-shadow: 0 1px 18px rgba(0, 14, 28, 0.34);
}

.login__story :deep(.brand-lockup__cn) {
  color: #ffffff;
}

.login__intro {
  margin: auto 0;
  padding: 64px 0;
}

.login__eyebrow {
  display: inline-block;
  margin-bottom: 18px;
  color: #7ce8f5;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 2.8px;
}

.login__intro h1 {
  max-width: 560px;
  color: #ffffff;
  font-size: clamp(38px, 4.2vw, 62px);
  font-weight: 700;
  line-height: 1.14;
  letter-spacing: 0;
}

.login__intro p {
  max-width: 460px;
  margin-top: 22px;
  color: rgba(255, 255, 255, 0.65);
  font-size: 14px;
  line-height: 1.8;
}

.login__story-meta {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
}

.login__story-foot {
  display: flex;
  align-items: center;
  gap: 9px;
  color: rgba(255, 255, 255, 0.5);
  font-size: 11.5px;
  letter-spacing: 0.35px;
}

.login__photo-credit {
  flex: 0 0 auto;
  padding: 3px 6px;
  border-radius: 6px;
  background: rgba(3, 20, 34, 0.28);
  color: rgba(255, 255, 255, 0.42);
  font-size: 9.5px;
  line-height: 1.4;
  text-align: right;
}

.login__pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #54e6bf;
  box-shadow: 0 0 0 5px rgba(84, 230, 191, 0.1);
}

.login__access {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-width: 0;
  padding: 40px 32px;
  background: var(--bg-page);
}

.login__card {
  width: min(100%, 444px);
  padding: 32px;
  border: 1px solid var(--panel-border);
  border-radius: 22px;
  background: var(--bg-panel);
  box-shadow: 0 18px 60px rgba(18, 42, 72, 0.09);
}

.login__head {
  margin-bottom: 25px;
}

.login__kicker {
  color: var(--c-primary);
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.8px;
}

.login__head h2 {
  margin-top: 6px;
  color: var(--text-main);
  font-size: 28px;
  font-weight: 700;
  letter-spacing: 0;
  line-height: 1.25;
}

.login__head p {
  margin-top: 7px;
  color: var(--text-dim);
  font-size: 13px;
}

.login__form {
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.login__field {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.login__field > span:first-child {
  color: var(--text-sub);
  font-size: 12px;
  font-weight: 550;
}

.login__field input {
  width: 100%;
  min-height: 48px;
  scroll-margin-block: 20px 120px;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-radius: 12px;
  outline: none;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 15px;
  transition:
    border-color var(--dur) var(--ease),
    background var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease);
}

.login__field input::placeholder {
  color: var(--text-dim);
}

.login__field input:focus {
  border-color: color-mix(in srgb, var(--c-primary) 70%, transparent);
  background: var(--bg-panel);
  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.1);
}

.login__password {
  position: relative;
  display: block;
}

.login__password input {
  padding-right: 62px;
}

.login__reveal {
  position: absolute;
  top: 50%;
  right: 8px;
  min-width: 48px;
  min-height: 34px;
  padding: 4px 7px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--c-primary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transform: translateY(-50%);
}

.login__reveal:hover {
  background: var(--bg-active);
}

.login__error {
  padding: 9px 11px;
  border-radius: 10px;
  background: rgba(255, 59, 48, 0.08);
  color: var(--c-danger);
  font-size: 12.5px;
}

.login__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-height: 50px;
  margin-top: 5px;
  padding: 11px 16px;
  border: 0;
  border-radius: 13px;
  background: var(--c-primary);
  box-shadow: 0 8px 20px rgba(0, 122, 255, 0.22);
  color: #ffffff;
  font-size: 15px;
  font-weight: 650;
  cursor: pointer;
  transition:
    background var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease),
    transform 0.12s var(--ease);
}

.login__btn:hover:not(:disabled) {
  background: var(--c-primary-dim);
  box-shadow: 0 10px 24px rgba(0, 122, 255, 0.27);
}

.login__btn:active:not(:disabled) {
  transform: scale(0.985);
}

.login__btn:disabled {
  opacity: 0.64;
  cursor: not-allowed;
}

.login__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.38);
  border-top-color: #ffffff;
  border-radius: 50%;
  animation: login-spin 0.8s linear infinite;
}

.login__foot {
  margin-top: 18px;
  color: var(--text-dim);
  font-size: 11px;
  text-align: center;
}

@keyframes login-spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 1100px) {
  .login {
    grid-template-columns: minmax(360px, 0.8fr) minmax(480px, 1.2fr);
  }

  .login__story-inner {
    padding: 44px;
  }

  .login__intro h1 {
    font-size: clamp(34px, 4vw, 48px);
  }
}

@media (max-width: 860px) {
  .login {
    display: block;
    height: 100vh;
    height: 100dvh;
    min-height: 0;
    background: var(--bg-page);
    touch-action: pan-y;
  }

  .login__story {
    min-height: 238px;
    border-radius: 0 0 28px 28px;
  }

  .login__story-inner {
    width: 100%;
    min-height: 238px;
    padding: 28px 22px 24px;
  }

  .login__intro {
    margin: 30px 0 0;
    padding: 0;
  }

  .login__eyebrow {
    display: none;
  }

  .login__intro h1 {
    font-size: 30px;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .login__intro p,
  .login__story-foot {
    display: none;
  }

  .login__story-meta {
    position: absolute;
    right: 22px;
    bottom: 18px;
    left: 22px;
  }

  .login__photo-credit {
    margin-left: auto;
    color: rgba(255, 255, 255, 0.48);
    font-size: 9px;
  }

  .login__access {
    justify-content: flex-start;
    padding: 18px 16px calc(28px + env(safe-area-inset-bottom, 0px));
  }

  .login__card {
    width: min(100%, 520px);
    padding: 25px 24px;
    border-radius: 18px;
  }
}

@media (max-width: 560px) {
  .login__story {
    min-height: 210px;
    border-radius: 0 0 24px 24px;
  }

  .login__story-inner {
    min-height: 210px;
    padding: 24px 18px 22px;
  }

  .login__story-meta {
    right: 18px;
    bottom: 14px;
    left: 18px;
  }

  .login__story :deep(.brand-lockup__cn) {
    font-size: 27px;
  }

  .login__story :deep(.brand-lockup__en) {
    font-size: 9px;
    letter-spacing: 2px;
  }

  .login__story :deep(.brand-lockup__sub) {
    font-size: 10px;
  }

  .login__intro {
    margin-top: 24px;
  }

  .login__intro h1 {
    font-size: 26px;
  }

  .login__access {
    padding-inline: 12px;
  }

  .login__card {
    padding: 23px 18px 20px;
  }

  .login__head h2 {
    font-size: 25px;
  }

  .login__field input {
    min-height: 50px;
    font-size: 16px;
  }
}

@media (max-width: 360px) {
  .login__story {
    min-height: 194px;
  }

  .login__story-inner {
    min-height: 194px;
    padding-inline: 15px;
  }

  .login__story :deep(.brand-lockup) {
    gap: 9px;
  }

  .login__story :deep(.brand-lockup__cn) {
    font-size: 24px;
  }

  .login__intro {
    margin-top: 20px;
  }

  .login__intro h1 {
    font-size: 23px;
  }

  .login__card {
    padding-inline: 15px;
  }
}
</style>
