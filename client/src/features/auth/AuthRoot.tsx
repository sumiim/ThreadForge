import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, ConfigProvider, Spin } from 'antd'
import { GithubOutlined, ReloadOutlined } from '@ant-design/icons'
import App from '../../App'
import Logo from '../../components/Logo'
import { friendlyMessage, getAuthStatus, githubLoginUrl, logout } from '../../api/client'
import type { AuthStatus } from '../../api/types'
import { useTheme } from '../../hooks/useTheme'
import { darkThemeConfig, themeConfig } from '../../styles/theme'

const loginErrors: Record<string, string> = {
  authentication_cancelled: 'GitHub 登录已取消',
  authorization_denied: '当前 GitHub 账户不在访问白名单中',
  oauth_state_invalid: '登录请求已失效，请重新发起登录',
  oauth_provider_error: 'GitHub 登录服务暂时不可用，请稍后重试',
}

export default function AuthRoot() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState('')
  const [signingOut, setSigningOut] = useState(false)
  const { mode } = useTheme()
  const loginErrorCode = new URLSearchParams(window.location.search).get('auth_error') ?? ''
  const loginError = loginErrors[loginErrorCode] ?? (loginErrorCode ? 'GitHub 登录失败' : '')

  const load = useCallback(async () => {
    setError('')
    try {
      setStatus(await getAuthStatus())
    } catch (cause) {
      setError(friendlyMessage(cause))
    }
  }, [])

  useEffect(() => {
    let active = true
    getAuthStatus().then(
      (next) => {
        if (active) setStatus(next)
      },
      (cause) => {
        if (active) setError(friendlyMessage(cause))
      },
    )
    return () => {
      active = false
    }
  }, [])

  const handleLogout = useCallback(async () => {
    setSigningOut(true)
    try {
      await logout()
      await load()
    } catch (cause) {
      setStatus(null)
      setError(friendlyMessage(cause))
    } finally {
      setSigningOut(false)
    }
  }, [load])

  if (status && (!status.authentication_required || status.authenticated)) {
    return <App auth={status} onLogout={handleLogout} signingOut={signingOut} />
  }

  return (
    <ConfigProvider theme={mode === 'dark' ? darkThemeConfig : themeConfig}>
      <main className="flex h-full min-h-[420px] items-center justify-center bg-stone-50 px-6">
        <section className="w-full max-w-sm text-center" aria-labelledby="login-title">
          <div className="mb-7 flex items-center justify-center gap-3">
            <Logo size={34} />
            <h1 id="login-title" className="text-2xl font-semibold text-stone-900">
              ThreadForge
            </h1>
          </div>
          {error ? (
            <>
              <Alert type="error" showIcon message="无法连接 ThreadForge" description={error} />
              <Button className="mt-5" icon={<ReloadOutlined />} onClick={() => void load()}>
                重新连接
              </Button>
            </>
          ) : status ? (
            <>
              {loginError ? (
                <Alert className="mb-5 text-left" type="error" showIcon message={loginError} />
              ) : null}
              <p className="mb-5 text-sm text-stone-500">使用已获准的 GitHub 账户继续</p>
              <Button
                type="primary"
                size="large"
                icon={<GithubOutlined />}
                onClick={() => window.location.assign(githubLoginUrl())}
              >
                使用 GitHub 登录
              </Button>
            </>
          ) : (
            <Spin size="large" aria-label="正在检查登录状态" />
          )}
        </section>
      </main>
    </ConfigProvider>
  )
}
