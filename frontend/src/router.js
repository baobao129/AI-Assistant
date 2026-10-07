import { createRouter, createWebHistory } from 'vue-router'
import { session } from './api'

const routes = [
  { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
  { path: '/login', name: 'login', component: () => import('./views/LoginView.vue') },
  { path: '/chat', name: 'chat', component: () => import('./views/ChatView.vue') },
  { path: '/data', name: 'data', component: () => import('./views/DataView.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局前置守卫：未登录访问受保护页面时重定向到 login，并带上原始路径以便登录后回跳。
router.beforeEach((to) => {
  if (to.name === 'login') return true
  if (!session.authenticated) return { name: 'login', query: { redirect: to.fullPath } }
  return true
})

export default router
