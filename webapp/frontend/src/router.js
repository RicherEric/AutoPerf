import { createRouter, createWebHistory } from 'vue-router'
import RunListView from './views/RunListView.vue'
import RunDetailView from './views/RunDetailView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'runs', component: RunListView },
    { path: '/runs/:id', name: 'run-detail', component: RunDetailView, props: true },
  ],
})
