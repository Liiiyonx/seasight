<template>
  <div class="arm-console">
    <div class="arm-console__head">
      <div class="arm-console__title">
        <span class="arm-console__eyebrow">机械臂执行端</span>
        <strong>{{ consoleLabel }}</strong>
        <span class="arm-console__tag" :class="{ 'is-ready': isReady }">
          {{ isReady ? '已配置跳转' : '待接入真机' }}
        </span>
      </div>
      <button
        class="arm-console__open"
        type="button"
        :disabled="!isReady"
        @click="openConsole"
      >
        <span aria-hidden="true">↗</span>
        打开仿真台
      </button>
    </div>

    <div class="arm-console__body">
      <div class="arm-console__row">
        <span>跳转目标</span>
        <code>{{ consoleTargetDisplay }}</code>
      </div>
      <div class="arm-console__row">
        <span>真机热点</span>
        <code>192.168.149.1</code>
      </div>
      <div class="arm-console__row">
        <span>登录账号</span>
        <code>ubuntu / hiwonder</code>
      </div>
      <div class="arm-console__row">
        <span>证据等级</span>
        <code>E2 受控实验</code>
      </div>
    </div>

    <div class="arm-console__foot">
      <button
        class="arm-console__copy"
        type="button"
        :class="{ 'is-copied': copied }"
        @click="copyConnection"
      >
        <span aria-hidden="true">{{ copied ? '✓' : '⧉' }}</span>
        {{ copied ? '已复制' : '复制连接信息' }}
      </button>
      <span class="arm-console__footnote">
        ArmPiFPV 树莓派远程桌面，不走平台 MQTT 闭环
      </span>
    </div>

    <details v-if="!isReady" class="arm-console__setup">
      <summary>接入前准备</summary>
      <p>
        在 <code>frontend/.env.local</code> 配置
        <code>VITE_ARM_CONSOLE_URL</code> 后，本按钮即变为可用。
      </p>
    </details>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const consoleLabel =
  import.meta.env.VITE_ARM_CONSOLE_LABEL || 'ArmPiFPV 仿真台'
const consoleUrl = String(import.meta.env.VITE_ARM_CONSOLE_URL || '').trim()
const copied = ref(false)

const isReady = computed(() => Boolean(consoleUrl))
const consoleTargetDisplay = computed(() => consoleUrl || '未配置')

function openConsole() {
  if (!consoleUrl) return
  window.open(consoleUrl, '_blank', 'noopener,noreferrer')
}

async function copyConnection() {
  const text = [
    'ArmPiFPV 远程桌面',
    '地址：192.168.149.1',
    '账号：ubuntu / 密码：hiwonder',
  ].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 1600)
  } catch {
    copied.value = false
  }
}
</script>

<style scoped>
.arm-console {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  overflow: auto;
  background:
    linear-gradient(145deg, color-mix(in srgb, var(--bg-panel-2) 55%, transparent), transparent 46%),
    var(--bg-panel);
}

.arm-console__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--separator);
  border-radius: 12px;
  background: var(--bg-panel-2);
}

.arm-console__title {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.arm-console__eyebrow {
  color: var(--text-dim);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0;
}

.arm-console__title strong {
  color: var(--text-main);
  font-size: 17px;
  line-height: 1.25;
}

.arm-console__tag {
  justify-self: start;
  margin-top: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(255, 149, 0, 0.14);
  color: var(--c-warn);
  font-size: 10.5px;
  font-weight: 600;
  white-space: nowrap;
}

.arm-console__tag.is-ready {
  background: rgba(52, 199, 89, 0.14);
  color: var(--c-success);
}

.arm-console__open {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-width: 118px;
  min-height: 40px;
  padding: 6px 14px;
  border: 0;
  border-radius: 9px;
  background: var(--c-primary);
  color: #ffffff;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.arm-console__open:hover:not(:disabled) {
  background: var(--c-primary-dim);
}

.arm-console__open:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.arm-console__body {
  display: grid;
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--separator);
  border-radius: 12px;
  background: var(--separator);
}

.arm-console__row {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-height: 44px;
  padding: 9px 12px;
  background: var(--bg-panel);
}

.arm-console__row span {
  color: var(--text-sub);
  font-size: 11.5px;
}

.arm-console__row code {
  overflow: hidden;
  color: var(--text-main);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 11px;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.arm-console__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 46px;
  padding: 8px 10px;
  border: 1px solid var(--separator);
  border-radius: 12px;
  background: var(--bg-panel-2);
}

.arm-console__copy {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 4px 10px;
  border: 0;
  border-radius: 8px;
  background: var(--bg-panel);
  color: var(--text-sub);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}

.arm-console__copy:hover,
.arm-console__copy.is-copied {
  color: var(--c-primary);
}

.arm-console__footnote {
  color: var(--text-dim);
  font-size: 10.5px;
  text-align: right;
}

.arm-console__setup {
  margin-top: auto;
  padding: 10px 12px;
  border: 1px dashed var(--border-bright);
  border-radius: 10px;
  color: var(--text-sub);
  font-size: 11px;
}

.arm-console__setup summary {
  cursor: pointer;
  color: var(--text-sub);
  font-weight: 600;
}

.arm-console__setup p {
  margin: 8px 0 0;
  line-height: 1.6;
}

.arm-console__setup code {
  color: var(--text-main);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
}

@media (max-width: 560px) {
  .arm-console {
    padding: 12px;
  }

  .arm-console__head {
    align-items: stretch;
    flex-direction: column;
  }

  .arm-console__open {
    width: 100%;
  }

  .arm-console__foot {
    align-items: stretch;
    flex-direction: column;
  }

  .arm-console__footnote {
    text-align: left;
  }
}
</style>
