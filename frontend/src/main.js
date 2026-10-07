import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

// 创建应用实例，安装路由后挂载到 #app 根节点。
createApp(App).use(router).mount('#app')
