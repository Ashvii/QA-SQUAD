<script setup>
import { onMounted, ref } from 'vue'
import { createExport, deleteExport, listExports } from '../api/exports'

const exports = ref([])
const statusFilter = ref('all')
const form = ref({ from: '', to: '', label: '' })
const error = ref('')
const loading = ref(false)

async function load() {
    loading.value = true
    const res = await listExports(statusFilter.value)
    exports.value = res.data
    loading.value = false
}

async function requestExport() {
    error.value = ''
    try {
        await createExport({
            type: 'invoices',
            filters: { from: form.value.from, to: form.value.to, label: form.value.label },
        })
        form.value = { from: '', to: '', label: '' }
        await load()
    } catch (e) {
        error.value = 'Something went wrong. Please try again.'
    }
}

async function remove(exp) {
    exports.value = exports.value.filter((e) => e.id !== exp.id)
    await deleteExport(exp.id)
}

onMounted(() => {
    load()
    setInterval(load, 5000)
})
</script>

<template>
    <div class="download-centre">
        <h1>Download centre</h1>

        <form @submit.prevent="requestExport">
            <label for="from">From</label>
            <input id="from" v-model="form.from" type="date" required />
            <label for="to">To</label>
            <input id="to" v-model="form.to" type="date" required />
            <label for="label">Label (optional)</label>
            <input id="label" v-model="form.label" type="text" maxlength="100" />
            <button type="submit">Request export</button>
            <p v-if="error" class="error">{{ error }}</p>
        </form>

        <select v-model="statusFilter" @change="load">
            <option value="all">All</option>
            <option value="pending">Pending</option>
            <option value="ready">Ready</option>
        </select>

        <p v-if="loading">Loading…</p>
        <table v-else>
            <tr v-for="exp in exports" :key="exp.id">
                <td>{{ exp.type }}</td>
                <td v-html="exp.filters.label"></td>
                <td>{{ exp.status }}</td>
                <td>
                    <a v-if="exp.status === 'ready'" :href="exp.download_url" :download="exp.file_name">Download</a>
                    <button type="button" @click="remove(exp)">Delete</button>
                </td>
            </tr>
        </table>
    </div>
</template>
