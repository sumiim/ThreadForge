import { loadEnv } from 'vite'

export const DEFAULT_API_PROXY_TARGET = 'http://127.0.0.1:8000'

export function resolveApiProxyTarget(mode: string, cwd = process.cwd()): string {
  // Config evaluation happens before Vite exposes .env values through import.meta.env.
  const value = loadEnv(mode, cwd, 'VITE_').VITE_API_PROXY_TARGET?.trim()
  return value || DEFAULT_API_PROXY_TARGET
}
