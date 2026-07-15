<script setup>
</script>

<template>
  <main>
    <h1><router-link to="/">AutoPerf Dashboard</router-link></h1>
    <router-view />
  </main>
</template>

<style>
body {
  font-family: system-ui, sans-serif;
  margin: 2rem;
  color: #1a1a1a;
}
table {
  border-collapse: collapse;
  width: 100%;
}
th, td {
  text-align: left;
  padding: 0.4rem 0.8rem;
  border-bottom: 1px solid #ddd;
}
button {
  padding: 0.4rem 0.8rem;
  cursor: pointer;
}
.error {
  color: #b00020;
}
</style>
