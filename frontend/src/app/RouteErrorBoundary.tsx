import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 捕获子路由渲染异常，输出页面内容或局部错误/重试界面。
 * 边界位于应用外壳与全局导航内部区域，页面异常不会卸载导航；路由路径变化时由父层重建。
 */
export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, _info: ErrorInfo): void {
    console.error('路由页面渲染失败', error);
  }

  private retry = (): void => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <section role="alert" style={{ padding: 24 }}>
          <h2>页面加载失败</h2>
          <p>{this.state.error.message || '页面暂时无法显示，请重试。'}</p>
          <button type="button" onClick={this.retry}>重试</button>
        </section>
      );
    }
    return this.props.children;
  }
}
