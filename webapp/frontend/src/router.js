import { createRouter, createWebHistory } from 'vue-router'
import StatsView from './views/StatsView.vue'
import RunListView from './views/RunListView.vue'
import RunDetailView from './views/RunDetailView.vue'
import QueueView from './views/QueueView.vue'
import DeviceScreenView from './views/DeviceScreenView.vue'
import JoinView from './views/JoinView.vue'
import MissionControlView from './views/MissionControlView.vue'
import CampaignsView from './views/CampaignsView.vue'
import DemoView from './views/DemoView.vue'
import { CLASSROOM_JOIN_ENABLED, TASK_QUEUE_ENABLED } from './features.js'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'stats', component: StatsView },
    { path: '/runs', name: 'runs', component: RunListView },
    { path: '/runs/:id', name: 'run-detail', component: RunDetailView, props: true },
    { path: '/campaigns', name: 'campaigns', component: CampaignsView },
    ...(TASK_QUEUE_ENABLED
      ? [{ path: '/queue', name: 'queue', component: QueueView }]
      : []),
    { path: '/demo', name: 'demo', component: DemoView },
    { path: '/screen', name: 'device-screen', component: DeviceScreenView },
    { path: '/mission-control', name: 'mission-control', component: MissionControlView },
    // Routed only when shown: a hidden page still reachable by typing its URL
    // is the version of "hidden" that gets demoed by accident.
    ...(CLASSROOM_JOIN_ENABLED
      ? [{ path: '/join', name: 'join', component: JoinView }]
      : []),
  ],
})
