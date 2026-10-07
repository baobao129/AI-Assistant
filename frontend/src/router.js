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

router.beforeEach((to) => {
  if (to.name === 'login') return true
  if (!session.authenticated) return { name: 'login', query: { redirect: to.fullPath } }
  return true
})

export default router
