import { ErrorState } from '@fusion/web-ui';
import { Component, type ErrorInfo, type ReactNode } from 'react';

// #413: router-level boundary — one page's render error must not unmount the
// whole module (a legacy agent with model-as-object blanked the embedded
// shell). Keyed by route in App so navigation resets the boundary.
interface Props {
    children: ReactNode;
}

interface State {
    error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
    state: State = { error: null };

    static getDerivedStateFromError(error: Error): State {
        return { error };
    }

    componentDidCatch(error: Error, info: ErrorInfo) {
        console.error('page render failed', error, info.componentStack);
    }

    render() {
        if (this.state.error) {
            return (
                <div className="px-4 py-6">
                    <ErrorState
                        message={`页面渲染失败 / Page failed to render: ${this.state.error.message}`}
                        retryLabel="重试 / Retry"
                        onRetry={() => this.setState({ error: null })}
                    />
                </div>
            );
        }
        return this.props.children;
    }
}
