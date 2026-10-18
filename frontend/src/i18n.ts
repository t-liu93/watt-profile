export const resources = {
  en: {
    app: 'Watt Profile Server', workspaceEyebrow: 'WATT PROFILE / WORKSPACE', accessEyebrow: 'WATT PROFILE / ACCESS', workbench: 'PRECISION WORKBENCH', sites: 'Sites', devices: 'Devices', registration: 'Registration',
    menu: 'Navigation', close: 'Close navigation', language: 'Language', dark: 'Switch to dark mode', light: 'Switch to light mode',
    login: 'Server login', username: 'Username', password: 'Password', submit: 'Log in', logout: 'Log out',
    loginHelp: 'Use the Server administrator account. Logger operator authentication is separate.',
    reloadLogin: 'Reload login form', skip: 'Skip to content', shell: 'Workspace ready',
    pending: 'This workspace section is ready for the next implementation step.',
    site: 'Site', device: 'Device', notFound: 'Page not found', home: 'Back to sites', checking: 'Checking session…',
    retry: 'Retry', connected: 'Administrator session active',
    errors: {
      required: 'Enter a username and password.',
      invalid_credentials: 'Administrator credentials are invalid. Retry with the Server account.',
      csrf_rejected: 'Login form expired or verification failed. Reload the login form and retry.',
      login_throttled: 'Too many login attempts. Wait one minute, reload the login form and retry.',
      authentication_required: 'Login expired. Log in again to continue.',
      invalid_input: 'Check the form values and try again.', object_not_found: 'The requested item was not found.',
      registration_conflict: 'Registration conflicts with the current Server state.',
      temporarily_unavailable: 'Server is temporarily unavailable. Try again.',
      connection: 'Connection failed. Try again.', unknown: 'The request could not be completed. Try again.',
    },
  },
  zh: {
    app: 'Watt Profile 服务器', workspaceEyebrow: 'WATT PROFILE / 工作区', accessEyebrow: 'WATT PROFILE / 访问', workbench: '精密工作台', sites: '站点', devices: '设备', registration: '注册',
    menu: '导航', close: '关闭导航', language: '语言', dark: '切换到深色模式', light: '切换到浅色模式',
    login: '服务器登录', username: '用户名', password: '密码', submit: '登录', logout: '退出登录',
    loginHelp: '请使用服务器管理员账户。Logger 操作员身份验证与此独立。',
    reloadLogin: '重新加载登录表单', skip: '跳转到内容', shell: '工作区已就绪',
    pending: '此工作区部分已准备好，可进行下一步实现。',
    site: '站点', device: '设备', notFound: '页面不存在', home: '返回站点', checking: '正在检查会话…',
    retry: '重试', connected: '管理员会话有效',
    errors: {
      required: '请输入用户名和密码。',
      invalid_credentials: '管理员凭据无效。请使用服务器账户重试。',
      csrf_rejected: '登录表单已过期或验证失败。请重新加载登录表单后重试。',
      login_throttled: '登录尝试过多。请等待一分钟，重新加载登录表单后重试。',
      authentication_required: '登录已过期。请重新登录以继续。',
      invalid_input: '请检查表单内容后重试。', object_not_found: '未找到请求的项目。',
      registration_conflict: '注册信息与服务器当前状态冲突。',
      temporarily_unavailable: '服务器暂时不可用。请重试。',
      connection: '连接失败。请重试。', unknown: '无法完成请求。请重试。',
    },
  },
} as const
export type Language = keyof typeof resources
export function initialLanguage(): Language {
  try { const value = localStorage.getItem('watt-profile-language'); if (value === 'en' || value === 'zh') return value } catch { /* preferences are optional */ }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}
export function initialTheme(): 'dark' | 'light' {
  try { if (localStorage.getItem('watt-profile-theme') === 'light') return 'light' } catch { /* preferences are optional */ }
  return 'dark'
}
export function savePreference(key: 'theme' | 'language', value: string) {
  try { localStorage.setItem(`watt-profile-${key}`, value) } catch { /* private browsing */ }
}
