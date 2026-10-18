import axios from 'axios'

export function listExports(status) {
    const params = status === 'all' ? {} : { status }

    return axios.get('/api/exports', { params })
}

export function createExport(payload) {
    return axios.post('/api/exports', payload)
}

export function deleteExport(id) {
    return axios.delete(`/api/exports/${id}`)
}
