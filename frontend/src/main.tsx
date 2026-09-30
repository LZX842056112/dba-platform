import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import './styles/tokens.css';
import './styles/global.css';

const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('未找到 #root 挂载点');
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
