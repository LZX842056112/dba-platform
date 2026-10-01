// 登录页（本地/浏览器联调最小集）：用户名 + 口令 → 存 JWT → 跳对话页。
//
// 稳定选择器（浏览器 e2e 依赖，勿随意改类名）：
//   .login-form / input[name=username] / input[name=password]
//   button[type=submit] / .hint--error

import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';

import { loginRequest } from '../../api/auth';
import { ApiError } from '../../api/client';
import { login } from '../../app/providers/AuthProvider';

export function LoginPage() {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await loginRequest(username, password);
      login(result.access_token);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后重试');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app-shell">
      <main className="app-main app-main--center">
        <form className="login-form panel" onSubmit={onSubmit}>
          <h2 className="panel-title">登录 · 数据大屏平台</h2>
          <label className="login-field">
            <span>用户名</span>
            <input
              name="username"
              value={username}
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className="login-field">
            <span>口令</span>
            <input
              name="password"
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error !== null && <div className="hint hint--error">{error}</div>}
          <button type="submit" disabled={busy}>
            {busy ? '登录中…' : '登录'}
          </button>
        </form>
      </main>
    </div>
  );
}
