// 面板级错误边界：单个面板渲染异常时只降级**该面板**，绝不整页白屏。
//
// 为什么需要：ECharts / 数据异常会让某个面板抛错；若没有边界，React 会卸载整棵树，
// 用户看到的是全空白页面（实测过：地图系列配置错误就是把整个对话页打没了）。

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  panelId: string;
  children: ReactNode;
}

interface State {
  message: string | null;
}

export class PanelErrorBoundary extends Component<Props, State> {
  state: State = { message: null };

  static getDerivedStateFromError(error: unknown): State {
    return { message: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('[dba] 面板渲染失败', this.props.panelId, error, info);
  }

  render(): ReactNode {
    if (this.state.message !== null) {
      return <div className="dim">面板渲染失败：{this.state.message}</div>;
    }
    return this.props.children;
  }
}
