import { createRouter, createWebHistory } from 'vue-router'
import StatsView from './views/StatsView.vue'
import RunListView from './views/RunListView.vue'
import RunDetailView from './views/RunDetailView.vue'
import QueueView from './views/QueueView.vue'
import DeviceScreenView from './views/DeviceScreenView.vue'
import JoinView from './views/JoinView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'stats', component: StatsView },
    { path: '/runs', name: 'runs', component: RunListView },
    { path: '/runs/:id', name: 'run-detail', component: RunDetailView, props: true },
    { path: '/queue', name: 'queue', component: QueueView },
    { path: '/screen', name: 'device-screen', component: DeviceScreenView },
    { path: '/join', name: 'join', component: JoinView },
  ],
})
