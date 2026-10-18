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
    useReactFlow,
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

type FusionNodeData = {
    label: string;
    nodeType: string;
    config: ServerNode;
    pinned?: boolean;
    breakpoint?: boolean;
};

const CANVAS_GRAPH_KEY = 'fusion.canvas.graphId';
const PIN_KEY_PREFIX = 'fusion.canvas.pins.';

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
            <div className="font-medium text-text-primary flex items-center gap-1">
                {data.label}
                {data.pinned ? <span title="data pinned">📌</span> : null}
                {data.breakpoint ? <span title="breakpoint">⏸</span> : null}
            </div>
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
    const [pins, setPins] = useState<Record<string, { output: string; ts: number }>>({});
    const [budget, setBudget] = useState<Record<string, number> | null>(null);
    const [pending, setPending] = useState<Array<Record<string, unknown>>>([]);
    const [connectMenu, setConnectMenu] = useState<{
        x: number;
        y: number;
        from: string;
        query: string;
    } | null>(null);
    const [historyLen, setHistoryLen] = useState(0);
    const historyRef = useRef<Array<{ nodes: Node[]; edges: Edge[] }>>([]);
    const historyIndexRef = useRef(-1);
    const nodesRef = useRef<Node[]>([]);
    const edgesRef = useRef<Edge[]>([]);
    const { screenToFlowPosition } = useReactFlow();

    useEffect(() => {
        nodesRef.current = nodes;
    }, [nodes]);
    useEffect(() => {
        edgesRef.current = edges;
    }, [edges]);

    // N13 (#386): undo/redo — snapshot the pre-mutation graph state.
    const pushHistory = useCallback(() => {
        const hist = historyRef.current.slice(0, historyIndexRef.current + 1);
        hist.push({ nodes: nodesRef.current, edges: edgesRef.current });
        historyRef.current = hist;
        historyIndexRef.current = hist.length - 1;
        setHistoryLen(hist.length);
    }, []);

    const restoreHistory = useCallback(
        (index: number) => {
            const entry = historyRef.current[index];
            if (!entry) return;
            historyIndexRef.current = index;
            setHistoryLen(historyRef.current.length);
            setNodes(entry.nodes);
            setEdges(entry.edges);
        },
        [setNodes, setEdges]
    );

    const undo = useCallback(() => {
        if (historyIndexRef.current <= 0) return;
        restoreHistory(historyIndexRef.current - 1);
    }, [restoreHistory]);

    const redo = useCallback(() => {
        if (historyIndexRef.current >= historyRef.current.length - 1) return;
        restoreHistory(historyIndexRef.current + 1);
    }, [restoreHistory]);

    useEffect(() => {
        const onKey = (ev: KeyboardEvent) => {
            if (!(ev.metaKey || ev.ctrlKey) || ev.key.toLowerCase() !== 'z') return;
            ev.preventDefault();
            if (ev.shiftKey) redo();
            else undo();
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [undo, redo]);

    const refreshGovernance = useCallback(async () => {
        try {
            const [b, p] = await Promise.all([
                rpc<Record<string, number>>('budget.status', {}),
                rpc<{ actions?: Array<Record<string, unknown>> }>('safety.get_pending_actions', {}),
            ]);
            setBudget(b ?? null);
            setPending(p?.actions ?? []);
        } catch (e) {
            console.warn('[canvas] governance refresh failed', e);
        }
    }, []);

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
                const rfNodes = toRfNodes(g);
                const rfEdges = toRfEdges(g).map((e) => ({
                    ...e,
                    markerEnd: { type: MarkerType.ArrowClosed },
                }));
                setGraph(g);
                setNodes(rfNodes);
                setEdges(rfEdges);
                setGovEvents([]);
                setActiveNodes({});
                nodesRef.current = rfNodes;
                edgesRef.current = rfEdges;
                historyRef.current = [{ nodes: rfNodes, edges: rfEdges }];
                historyIndexRef.current = 0;
                setHistoryLen(1);
                try {
                    const raw = window.localStorage.getItem(`${PIN_KEY_PREFIX}${id}`);
                    setPins(
                        raw
                            ? (JSON.parse(raw) as Record<string, { output: string; ts: number }>)
                            : {}
                    );
                } catch {
                    setPins({});
                }
                void refreshGovernance();
            } catch (e) {
                showToast(`${t('加载工作流失败', 'Failed to load workflow')}: ${String(e)}`);
            }
        },
        [setNodes, setEdges, showToast, t, refreshGovernance]
    );

    useEffect(() => {
        try {
            const preselected = window.sessionStorage.getItem(CANVAS_GRAPH_KEY);
            if (preselected) {
                window.sessionStorage.removeItem(CANVAS_GRAPH_KEY);
                setGraphId(preselected);
                void loadGraph(preselected);
            }
        } catch {
            // storage unavailable
        }
    }, [loadGraph]);

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
            pushHistory();
            setEdges((eds) => addEdge({ ...c, markerEnd: { type: MarkerType.ArrowClosed } }, eds));
        },
        [graph, edges, setEdges, showToast, t, pushHistory]
    );

    // N13: drag from a source handle onto empty canvas opens a searchable
    // node palette; picking a type creates the node and connects it.
    const onConnectEnd = useCallback(
        (
            event: MouseEvent | TouchEvent,
            connectionState: { fromNode?: Node | null; toNode?: Node | null }
        ) => {
            if (!connectionState?.fromNode || connectionState.toNode) return;
            const point = 'clientX' in event ? event : event.changedTouches?.[0];
            if (!point) return;
            setConnectMenu({
                x: point.clientX,
                y: point.clientY,
                from: connectionState.fromNode.id,
                query: '',
            });
        },
        []
    );

    const addNodeFromMenu = useCallback(
        (nodeType: string) => {
            if (!connectMenu) return;
            const id = `${nodeType}_${Math.random().toString(36).slice(2, 6)}`;
            const position = screenToFlowPosition({ x: connectMenu.x, y: connectMenu.y });
            const serverEdges = edges.map((e) => ({ source_id: e.source, target_id: e.target }));
            const check = canConnect(graph, connectMenu.from, id, serverEdges);
            const newNode: Node = {
                id,
                type: 'fusionNode',
                position,
                data: { label: id, nodeType, config: { type: nodeType, label: id } },
            };
            pushHistory();
            setNodes((ns) => [...ns, newNode]);
            if (check.ok) {
                setEdges((eds) =>
                    addEdge(
                        {
                            id: `${connectMenu.from}-${id}`,
                            source: connectMenu.from,
                            target: id,
                            markerEnd: { type: MarkerType.ArrowClosed },
                        },
                        eds
                    )
                );
            } else {
                showToast(`${t('节点已添加但未连线', 'Node added without edge')}: ${check.reason}`);
            }
            setConnectMenu(null);
        },
        [
            connectMenu,
            edges,
            graph,
            pushHistory,
            setNodes,
            setEdges,
            screenToFlowPosition,
            showToast,
            t,
        ]
    );

    const selectedNode = useMemo(
        () => nodes.find((n) => n.id === selectedId) as Node<FusionNodeData> | undefined,
        [nodes, selectedId]
    );

    const updateSelectedConfig = useCallback(
        (patch: Partial<FusionNodeData>) => {
            if (!selectedId) return;
            pushHistory();
            setNodes((ns) =>
                ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, ...patch } } : n))
            );
        },
        [selectedId, setNodes, pushHistory]
    );

    const removeSelected = useCallback(() => {
        if (!selectedId) return;
        pushHistory();
        setNodes((ns) => ns.filter((n) => n.id !== selectedId));
        setEdges((es) => es.filter((e) => e.source !== selectedId && e.target !== selectedId));
        setSelectedId('');
    }, [selectedId, setNodes, setEdges, pushHistory]);

    const addNode = useCallback(
        (nodeType: string) => {
            const id = `${nodeType}_${Math.random().toString(36).slice(2, 6)}`;
            pushHistory();
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
        [setNodes, pushHistory]
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

    const runWithStream = useCallback(
        async (opts: { fromNode?: string } = {}) => {
            if (!graphId || running) return;
            setRunning(true);
            setGovEvents([]);
            setActiveNodes({});
            void refreshGovernance();
            const qs = new URLSearchParams({ input: 'canvas run' });
            if (opts.fromNode) qs.set('from_node', opts.fromNode);
            const breakpointNode = nodes.find((n) => (n.data as FusionNodeData).breakpoint);
            if (breakpointNode) qs.set('stop_after', breakpointNode.id);
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
                        // N13 pin: capture node output snapshot for reuse.
                        const isPinned = nodesRef.current.some(
                            (n) => n.id === nodeId && (n.data as FusionNodeData).pinned
                        );
                        if (isPinned && (kind === 'result' || kind === 'tool_result')) {
                            const output = (data.content || '').slice(0, 4000);
                            setPins((prev) => {
                                const next = { ...prev, [nodeId]: { output, ts: Date.now() } };
                                try {
                                    window.localStorage.setItem(
                                        `${PIN_KEY_PREFIX}${graphId}`,
                                        JSON.stringify(next)
                                    );
                                } catch {
                                    // storage full/unavailable
                                }
                                return next;
                            });
                        }
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
        },
        [graphId, running, nodes, refreshGovernance]
    );

    useEffect(() => {
        if (!running) return;
        const timer = window.setInterval(() => void refreshGovernance(), 5000);
        return () => window.clearInterval(timer);
    }, [running, refreshGovernance]);

    const decide = useCallback(
        async (actionId: string, approve: boolean) => {
            try {
                await rpc(approve ? 'safety.approve' : 'safety.reject', { action_id: actionId });
                showToast(approve ? t('已批准', 'Approved') : t('已拒绝', 'Rejected'));
                void refreshGovernance();
            } catch (e) {
                showToast(`${t('审批失败', 'Approval failed')}: ${String(e)}`);
            }
        },
        [refreshGovernance, showToast, t]
    );

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
                <div className="flex gap-1 pt-1">
                    <button
                        type="button"
                        className="flex-1 rounded border border-border px-1 py-0.5 text-[10px] disabled:opacity-40"
                        disabled={historyIndexRef.current <= 0}
                        onClick={undo}
                        title="Cmd/Ctrl+Z"
                    >
                        ↶ {t('撤销', 'Undo')}
                    </button>
                    <button
                        type="button"
                        className="flex-1 rounded border border-border px-1 py-0.5 text-[10px] disabled:opacity-40"
                        disabled={historyIndexRef.current >= historyLen - 1}
                        onClick={redo}
                        title="Cmd/Ctrl+Shift+Z"
                    >
                        ↷ {t('重做', 'Redo')}
                    </button>
                </div>
            </div>
            <div className="flex-1 relative">
                <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    nodeTypes={nodeTypes}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onConnect={guardConnect}
                    onConnectEnd={onConnectEnd}
                    onNodeDragStart={pushHistory}
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
                {connectMenu && (
                    <div
                        className="absolute z-10 w-52 rounded border border-border bg-bg-elevated p-2 shadow-lg"
                        style={{ left: connectMenu.x, top: connectMenu.y }}
                    >
                        <input
                            autoFocus
                            className="mb-1 w-full rounded border border-border bg-bg px-1 py-0.5 text-xs"
                            placeholder={t('搜索节点类型…', 'Search node type…')}
                            value={connectMenu.query}
                            onChange={(e) =>
                                setConnectMenu({ ...connectMenu, query: e.target.value })
                            }
                        />
                        <div className="max-h-40 space-y-0.5 overflow-y-auto">
                            {NODE_TYPES.filter((nt) =>
                                nt.includes(connectMenu.query.trim().toLowerCase())
                            ).map((nt) => (
                                <button
                                    type="button"
                                    key={nt}
                                    className="block w-full rounded px-1 py-0.5 text-left text-[10px] hover:bg-bg-muted"
                                    onClick={() => addNodeFromMenu(nt)}
                                >
                                    + {nt}
                                </button>
                            ))}
                        </div>
                        <button
                            type="button"
                            className="mt-1 w-full rounded border border-border px-1 py-0.5 text-[10px]"
                            onClick={() => setConnectMenu(null)}
                        >
                            {t('取消', 'Cancel')}
                        </button>
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
                        <label className="flex items-center gap-1">
                            <input
                                type="checkbox"
                                checked={Boolean(selectedNode.data.pinned)}
                                onChange={(e) => updateSelectedConfig({ pinned: e.target.checked })}
                            />
                            {t('固定数据 (pin)', 'Pin data')}
                        </label>
                        <label className="flex items-center gap-1">
                            <input
                                type="checkbox"
                                checked={Boolean(selectedNode.data.breakpoint)}
                                onChange={(e) =>
                                    updateSelectedConfig({ breakpoint: e.target.checked })
                                }
                            />
                            {t('断点 (运行到此暂停)', 'Breakpoint (pause here)')}
                        </label>
                        {pins[selectedNode.id] ? (
                            <div className="rounded border border-border bg-bg p-1">
                                <div className="text-text-secondary">
                                    {t('已固定输出', 'Pinned output')}
                                </div>
                                <pre className="max-h-24 overflow-auto whitespace-pre-wrap text-[9px]">
                                    {pins[selectedNode.id].output}
                                </pre>
                            </div>
                        ) : null}
                        <button
                            type="button"
                            className="w-full rounded border border-brand px-2 py-1 text-brand disabled:opacity-40"
                            disabled={running || !graphId}
                            onClick={() => void runWithStream({ fromNode: selectedNode.id })}
                        >
                            {t('从此节点重跑下游', 'Rerun downstream from here')}
                        </button>
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
                {budget ? (
                    <div className="rounded border border-border bg-bg-elevated px-2 py-1 text-[10px] text-text-secondary">
                        {t('预算余量', 'Budget remaining')}:{' '}
                        <span className="text-brand">{String(budget.remaining ?? '∞')}</span> /{' '}
                        {String(budget.max_tokens ?? '∞')}
                    </div>
                ) : null}
                {pending.length > 0 ? (
                    <div className="space-y-1">
                        <div className="text-[10px] text-text-secondary">
                            {t('待审批', 'Pending approvals')} ({pending.length})
                        </div>
                        {pending.map((action, i) => {
                            const actionId = String(
                                action.action_id ?? action.id ?? action.request_id ?? `pending-${i}`
                            );
                            return (
                                <div
                                    key={actionId}
                                    className="rounded border border-warning/40 bg-bg-elevated px-1.5 py-1 text-[10px]"
                                >
                                    <div className="text-text-primary">
                                        {String(action.category ?? action.tool ?? 'action')}
                                    </div>
                                    <div className="mt-1 flex gap-1">
                                        <button
                                            type="button"
                                            className="rounded bg-brand px-1.5 py-0.5 text-white"
                                            onClick={() => void decide(actionId, true)}
                                        >
                                            {t('批准', 'Approve')}
                                        </button>
                                        <button
                                            type="button"
                                            className="rounded border border-error px-1.5 py-0.5 text-error"
                                            onClick={() => void decide(actionId, false)}
                                        >
                                            {t('拒绝', 'Deny')}
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : null}
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
