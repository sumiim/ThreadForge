function normalizeApiBaseUrl(value: string | undefined): string {
  const trimmed = value?.trim().replace(/\/+$/, '') ?? ''
  if (!trimmed) return ''
  try {
    const url = new URL(trimmed)
    if (!['http:', 'https:'].includes(url.protocol) || url.pathname !== '/' || url.search || url.hash) {
      return ''
    }
    return url.origin
  } catch {
    return ''
  }
}

const electronApiBase = typeof window === 'undefined' ? undefined : window.threadforge?.apiBaseUrl
const configuredApiBase = electronApiBase ?? import.meta.env?.VITE_API_BASE_URL

export const API_BASE_URL = normalizeApiBaseUrl(configuredApiBase)

export function apiUrl(path: string): string {
  if (!path.startsWith('/')) throw new Error('API path must start with /')
  return `${API_BASE_URL}${path}`
}
