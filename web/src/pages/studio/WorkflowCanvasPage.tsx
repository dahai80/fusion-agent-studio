// N13 (#352): graphical workflow canvas — React Flow over the single server
// graph JSON schema. Typed ports with connect-rejection, node inspector,
// and canvas-as-governance-panel (run SSE events stream onto the canvas).
import {
    Background,
    BackgroundVariant,
    type Connection,
    Controls,
    type Edge,
    Handle,
    MarkerType,
    MiniMap,
    type Node,
    type NodeChange,
    type NodeTypes,
    Position,
    ReactFlow,
    ReactFlowProvider,
    addEdge,
    useEdgesState,
    useNodesState,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiBase, rpc } from '../../api';
import { useT } from '../../i18n';
import {
    type GraphJson,
    NODE_TYPES,
    type ServerNode,
    canConnect,
    fromRf,
    inPortType,
    maxOutEdges,
    outPortType,
    toRfEdges,
    toRfNodes,
} from '../../lib/graphSchema';

const PORT_COLOR: Record<string, string> = {
    flow: '#6b7280',
    branch: '#d97706',
    fan: '#0ea5e9',
};

type FusionNodeData = { label: string; nodeType: string; config: ServerNode };

function FusionNode(props: { data: FusionNodeData; selected?: boolean }) {
    const { data, selected } = props;
    const inP = inPortType(data.nodeType);
    const outP = outPortType(data.nodeType);
    const cap = maxOutEdges(data.nodeType);
    return (
        <div
            className={`rounded-lg border px-3 py-2 min-w-[140px] bg-bg-elevated text-xs shadow-sm ${
                selected ? 'border-brand' : 'border-border'
            }`}
        >
            {inP !== null && (
                <Handle
                    type="target"
                    position={Position.Left}
                    data-port-type={inP}
                    style={{ backgroundColor: PORT_COLOR[inP], width: 12, height: 12 }}
                />
            )}
            <div className="font-medium text-text-primary">{data.label}</div>
            <div className="text-[10px] text-text-secondary mt-0.5 flex items-center gap-1">
                <span className="rounded bg-brand/10 text-brand px-1">{data.nodeType}</span>
                {data.config.tool_name ? <span>· {String(data.config.tool_name)}</span> : null}
                {data.config.model ? <span>· {String(data.config.model)}</span> : null}
            </div>
            {outP !== null && (
                <div className="text-[9px] text-text-secondary mt-1">
                    {cap === Number.POSITIVE_INFINITY
                        ? 'multi-out'
                        : cap === 1
                          ? 'single-out'
                          : 'no-out'}
                </div>
            )}
            {outP !== null && (
                <Handle
                    type="source"
                    position={Position.Right}
                    data-port-type={outP}
                    style={{ backgroundColor: PORT_COLOR[outP], width: 12, height: 12 }}
                />
            )}
        </div>
    );
}

const nodeTypes: NodeTypes = { fusionNode: FusionNode };

interface SseFrame {
    type?: string;
    node_id?: string;
    content?: string;
    name?: string;
    message?: string;
}

interface GovEvent {
    ts: number;
    kind: string;
    nodeId: string;
    detail: string;
}

function CanvasInner() {
    const t = useT();
    const [graphs, setGraphs] = useState<Array<{ graph_id: string; name: string }>>([]);
    const [graphId, setGraphId] = useState('');
    const [graph, setGraph] = useState<GraphJson>({});
    const [nodes, setNodes, onNodesChangeBase] = useNodesState<Node>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
    const [selectedId, setSelectedId] = useState('');
    const [toast, setToast] = useState('');
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [govEvents, setGovEvents] = useState<GovEvent[]>([]);
    const [activeNodes, setActiveNodes] = useState<Record<string, string>>({});
    const esRef = useRef<EventSource | null>(null);

    const showToast = useCallback((msg: string) => {
        setToast(msg);
        window.setTimeout(() => setToast(''), 2600);
    }, []);

    useEffect(() => {
        api.graphs()
            .then((res) => {
                setGraphs(res.data.map((g) => ({ graph_id: g.graph_id, name: g.name })));
            })
            .catch(() => showToast(t('加载画布列表失败', 'Failed to load graphs')));
    }, [showToast, t]);

    const loadGraph = useCallback(
        async (id: string) => {
            if (!id) return;
            try {
                const res = await rpc<{ graph?: GraphJson }>('graph.get', { graph_id: id });
                const g = res.graph || (res as unknown as GraphJson);
                setGraph(g);
                setNodes(toRfNodes(g));
                setEdges(
                    toRfEdges(g).map((e) => ({
                        ...e,
                        markerEnd: { type: MarkerType.ArrowClosed },
                    }))
                );
                setGovEvents([]);
                setActiveNodes({});
            } catch (e) {
                showToast(`${t('加载工作流失败', 'Failed to load workflow')}: ${String(e)}`);
            }
        },
        [setNodes, setEdges, showToast, t]
    );

    const onNodesChange = useCallback(
        (changes: NodeChange[]) => {
            onNodesChangeBase(changes);
            const sel = changes.find((c) => c.type === 'select' && c.selected);
            if (sel && 'id' in sel) setSelectedId(sel.id);
        },
        [onNodesChangeBase]
    );

    const guardConnect = useCallback(
        (c: Connection) => {
            const serverEdges = edges.map((e) => ({ source_id: e.source, target_id: e.target }));
            const check = canConnect(graph, c.source || '', c.target || '', serverEdges);
            if (!check.ok) {
                showToast(`${t('连接被拒绝', 'Connection rejected')}: ${check.reason}`);
                return;
            }
            setEdges((eds) => addEdge({ ...c, markerEnd: { type: MarkerType.ArrowClosed } }, eds));
        },
        [graph, edges, setEdges, showToast, t]
    );

    const selectedNode = useMemo(
        () => nodes.find((n) => n.id === selectedId) as Node<FusionNodeData> | undefined,
        [nodes, selectedId]
    );

    const updateSelectedConfig = useCallback(
        (patch: Partial<FusionNodeData>) => {
            if (!selectedId) return;
            setNodes((ns) =>
                ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, ...patch } } : n))
            );
        },
        [selectedId, setNodes]
    );

    const removeSelected = useCallback(() => {
        if (!selectedId) return;
        setNodes((ns) => ns.filter((n) => n.id !== selectedId));
        setEdges((es) => es.filter((e) => e.source !== selectedId && e.target !== selectedId));
        setSelectedId('');
    }, [selectedId, setNodes, setEdges]);

    const addNode = useCallback(
        (nodeType: string) => {
            const id = `${nodeType}_${Math.random().toString(36).slice(2, 6)}`;
            setNodes((ns) => [
                ...ns,
                {
                    id,
                    type: 'fusionNode',
                    position: { x: 80 + Math.random() * 220, y: 80 + Math.random() * 160 },
                    data: { label: id, nodeType, config: { type: nodeType, label: id } },
                },
            ]);
        },
        [setNodes]
    );

    const save = useCallback(async () => {
        if (!graphId) return;
        setSaving(true);
        try {
            const next = fromRf(
                graph,
                nodes as unknown as import('../../lib/graphSchema').RfNode[],
                edges as unknown as import('../../lib/graphSchema').RfEdge[]
            );
            await rpc('graph.update', { graph_id: graphId, graph_data: next });
            setGraph(next);
            showToast(t('已保存', 'Saved'));
        } catch (e) {
            showToast(`${t('保存失败', 'Save failed')}: ${String(e)}`);
        } finally {
            setSaving(false);
        }
    }, [graphId, graph, nodes, edges, showToast, t]);

    const runWithStream = useCallback(async () => {
        if (!graphId || running) return;
        setRunning(true);
        setGovEvents([]);
        setActiveNodes({});
        const qs = new URLSearchParams({ input: 'canvas run' });
        const cfgKey = import.meta.env.VITE_API_KEY as string | undefined;
        if (cfgKey) qs.set('api_key', cfgKey);
        const es = new EventSource(
            `${apiBase()}/v1/graphs/${encodeURIComponent(graphId)}/execute/stream?${qs.toString()}`
        );
        esRef.current = es;
        es.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data) as SseFrame;
                if (data.type === 'done') {
                    es.close();
                    setRunning(false);
                    setGovEvents((prev) => [
                        ...prev,
                        {
                            ts: Date.now(),
                            kind: 'done',
                            nodeId: '',
                            detail: data.message || 'ok',
                        },
                    ]);
                    return;
                }
                const kind = data.type || 'event';
                const nodeId = data.node_id || '';
                if (nodeId) {
                    setActiveNodes((m) => ({ ...m, [nodeId]: kind }));
                }
                setGovEvents((prev) =>
                    [
                        ...prev,
                        {
                            ts: Date.now(),
                            kind,
                            nodeId,
                            detail: (data.content || data.name || '').slice(0, 120),
                        },
                    ].slice(-200)
                );
            } catch {
                // ignore malformed frames
            }
        };
        es.onerror = () => {
            es.close();
            setRunning(false);
        };
    }, [graphId, running]);

    useEffect(() => () => esRef.current?.close(), []);

    return (
        <div className="flex h-full">
            <div className="w-56 border-r border-border p-3 space-y-2 overflow-y-auto">
                <div className="text-sm font-medium">{t('工作流画布', 'Workflow Canvas')}</div>
                <select
                    className="w-full rounded border border-border bg-bg px-2 py-1 text-xs"
                    value={graphId}
                    onChange={(e) => {
                        setGraphId(e.target.value);
                        void loadGraph(e.target.value);
                    }}
                >
                    <option value="">{t('选择工作流…', 'Select workflow…')}</option>
                    {graphs.map((g) => (
                        <option key={g.graph_id} value={g.graph_id}>
                            {g.name}
                        </option>
                    ))}
                </select>
                <div className="text-xs text-text-secondary pt-1">{t('添加节点', 'Add node')}</div>
                <div className="grid grid-cols-2 gap-1">
                    {NODE_TYPES.map((nt) => (
                        <button
                            type="button"
                            key={nt}
                            className="rounded border border-border px-1 py-0.5 text-[10px] hover:border-brand"
                            onClick={() => addNode(nt)}
                        >
                            + {nt}
                        </button>
                    ))}
                </div>
                <button
                    type="button"
                    className="w-full rounded bg-brand px-2 py-1 text-xs text-white disabled:opacity-50 mt-2"
                    disabled={!graphId || saving}
                    onClick={() => void save()}
                >
                    {saving ? t('保存中…', 'Saving…') : t('保存', 'Save')}
                </button>
                <button
                    type="button"
                    className="w-full rounded border border-brand px-2 py-1 text-xs text-brand disabled:opacity-50"
                    disabled={!graphId || running}
                    onClick={() => void runWithStream()}
                >
                    {running ? t('运行中…', 'Running…') : t('运行并监视', 'Run & watch')}
                </button>
            </div>
            <div className="flex-1 relative">
                <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    nodeTypes={nodeTypes}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onConnect={guardConnect}
                    fitView
                >
                    <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
                    <Controls />
                    <MiniMap pannable />
                </ReactFlow>
                {toast && (
                    <div className="absolute top-2 left-1/2 -translate-x-1/2 rounded bg-error px-3 py-1 text-xs text-white shadow">
                        {toast}
                    </div>
                )}
            </div>
            <div className="w-72 border-l border-border p-3 space-y-3 overflow-y-auto">
                <div className="text-sm font-medium">{t('节点检查器', 'Node Inspector')}</div>
                {selectedNode ? (
                    <div className="space-y-2 text-xs">
                        <div>
                            <div className="text-text-secondary">{t('名称', 'Label')}</div>
                            <input
                                className="w-full rounded border border-border bg-bg px-2 py-1"
                                value={selectedNode.data.label}
                                onChange={(e) => updateSelectedConfig({ label: e.target.value })}
                            />
                        </div>
                        <div>
                            <div className="text-text-secondary">{t('类型', 'Type')}</div>
                            <select
                                className="w-full rounded border border-border bg-bg px-2 py-1"
                                value={selectedNode.data.nodeType}
                                onChange={(e) =>
                                    updateSelectedConfig({
                                        nodeType: e.target.value,
                                        config: {
                                            ...selectedNode.data.config,
                                            type: e.target.value,
                                        },
                                    })
                                }
                            >
                                {NODE_TYPES.map((nt) => (
                                    <option key={nt} value={nt}>
                                        {nt}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <div className="text-text-secondary">
                                {t('配置 JSON', 'Config JSON')}
                            </div>
                            <textarea
                                className="w-full h-40 rounded border border-border bg-bg px-2 py-1 font-mono"
                                defaultValue={JSON.stringify(selectedNode.data.config, null, 4)}
                                onBlur={(e) => {
                                    try {
                                        const cfg = JSON.parse(e.target.value) as ServerNode;
                                        updateSelectedConfig({ config: cfg });
                                    } catch {
                                        showToast(t('JSON 解析失败', 'Invalid JSON'));
                                    }
                                }}
                            />
                        </div>
                        <button
                            type="button"
                            className="rounded border border-error px-2 py-1 text-error"
                            onClick={removeSelected}
                        >
                            {t('删除节点', 'Delete node')}
                        </button>
                    </div>
                ) : (
                    <div className="text-xs text-text-secondary">
                        {t('点击节点以编辑', 'Click a node to inspect')}
                    </div>
                )}
                <div className="text-sm font-medium pt-2">
                    {t('治理事件流', 'Governance events')}
                </div>
                {Object.keys(activeNodes).length > 0 && (
                    <div className="flex flex-wrap gap-1">
                        {Object.entries(activeNodes).map(([nid, kind]) => (
                            <span
                                key={nid}
                                className="rounded bg-brand/10 text-brand px-1 text-[10px]"
                                title={kind}
                            >
                                ● {nid}
                            </span>
                        ))}
                    </div>
                )}
                <div className="space-y-1 text-[10px] font-mono">
                    {govEvents.length === 0 && (
                        <div className="text-text-secondary">
                            {t(
                                '运行后显示 hook/权限/预算事件',
                                'Hook/permission/budget events appear on run'
                            )}
                        </div>
                    )}
                    {govEvents.map((ev, i) => (
                        <div key={i} className="rounded bg-bg-elevated px-1.5 py-1">
                            <span className="text-brand">{ev.kind}</span>
                            {ev.nodeId ? (
                                <span className="text-text-secondary"> @{ev.nodeId}</span>
                            ) : null}
                            {ev.detail ? (
                                <div className="text-text-secondary">{ev.detail}</div>
                            ) : null}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

export function WorkflowCanvasPage() {
    return (
        <div className="h-[calc(100vh-4rem)]">
            <ReactFlowProvider>
                <CanvasInner />
            </ReactFlowProvider>
        </div>
    );
}
