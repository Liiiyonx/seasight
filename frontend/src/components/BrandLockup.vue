<template>
  <div class="brand-lockup" :class="`brand-lockup--${variant}`">
    <LogoMark :size="size" />
    <div class="brand-lockup__copy">
      <div class="brand-lockup__title">
        <span class="brand-lockup__cn">探海灵眸</span>
        <span class="brand-lockup__en">Oceanus</span>
      </div>
      <span v-if="subtitle" class="brand-lockup__sub">{{ subtitle }}</span>
    </div>
  </div>
</template>

<script setup>
import LogoMark from '@/components/LogoMark.vue'

defineProps({
  size: { type: Number, default: 34 },
  variant: {
    type: String,
    default: 'header',
    validator: (value) => ['header', 'login', 'hero'].includes(value),
  },
  subtitle: { type: String, default: '' },
})
</script>

<style scoped>
.brand-lockup {
  display: inline-flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.brand-lockup__copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.brand-lockup__title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.brand-lockup__cn {
  color: var(--text-main);
  font-weight: 700;
  letter-spacing: 0.2px;
  white-space: nowrap;
}

.brand-lockup__en {
  color: var(--text-dim);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1.4px;
  line-height: 1;
  text-transform: uppercase;
  white-space: nowrap;
}

.brand-lockup__sub {
  color: var(--text-dim);
  font-size: 11px;
  letter-spacing: 0.2px;
  line-height: 1.35;
  white-space: nowrap;
}

.brand-lockup--header .brand-lockup__cn {
  font-size: 18px;
}

.brand-lockup--login .brand-lockup__copy,
.brand-lockup--hero .brand-lockup__copy {
  gap: 2px;
}

.brand-lockup--login .brand-lockup__cn {
  font-size: 24px;
}

.brand-lockup--hero .brand-lockup__cn {
  font-size: 31px;
}

.brand-lockup--hero .brand-lockup__en {
  font-size: 11px;
  letter-spacing: 2.6px;
}

.brand-lockup--hero .brand-lockup__sub {
  color: rgba(255, 255, 255, 0.64);
  font-size: 12px;
}

@media (max-width: 720px) {
  .brand-lockup--header .brand-lockup__en,
  .brand-lockup--header .brand-lockup__sub {
    display: none;
  }
}
</style>
