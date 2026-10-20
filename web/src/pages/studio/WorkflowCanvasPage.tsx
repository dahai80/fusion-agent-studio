// N13 (#352): graphical workflow canvas — React Flow over the single server
// graph JSON schema. Typed ports with connect-rejection, node inspector,
// and canvas-as-governance-panel (run SSE events stream onto the canvas).
// Langflow-parity batch (#393-#398, audit-1020): schema-driven config forms,
// sidebar drag-onto-canvas, pre-save validation, import/export + templates,
// edge labels, single-node test run, visual grouping.
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
import { ConfirmDialog } from '@fusion/web-ui';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiBase, rpc } from '../../api';
import { useT } from '../../i18n';
import {
    type FieldSpec,
    type GraphIssue,
    type GraphJson,
    NODE_TYPES,
    type RfEdge,
    type RfNode,
    type ServerNode,
    canConnect,
    fieldsForNode,
    fromRf,
    inPortType,
    maxOutEdges,
    outPortType,
    toRfEdges,
    toRfNodes,
    validateGraphClient,
} from '../../lib/graphSchema';

const PORT_COLOR: Record<string, string> = {
    flow: '#6b7280',
    branch: '#d97706',
    fan: '#0ea5e9',
};

const CANVAS_GRAPH_KEY = 'fusion.canvas.graphId';
const PIN_KEY_PREFIX = 'fusion.canvas.pins.';

// #394: sidebar palette split — execution flow vs intelligence nodes.
const NODE_GROUPS: Array<{ title: string; types: string[] }> = [
    {
        title: 'Execution flow',
        types: ['start', 'end', 'llm', 'tool', 'condition', 'loop', 'parallel', 'error_handler'],
    },
    { title: 'Intelligence', types: ['rag', 'planner', 'verify'] },
];

// #396: examples/*.json templates bundled at build time (no runtime disk read).
const TEMPLATES: Array<{ name: string; graph: GraphJson }> = (() => {
    const modules = import.meta.glob('../../../../examples/*.json', {
        eager: true,
        query: '?raw',
        import: 'default',
    });
    const out: Array<{ name: string; graph: GraphJson }> = [];
    for (const [path, raw] of Object.entries(modules)) {
        try {
            out.push({
                name: path.split('/').pop() || path,
                graph: JSON.parse(raw as string) as GraphJson,
            });
        } catch {
            // skip malformed template
        }
    }
    return out.sort((a, b) => a.name.localeCompare(b.name));
})();

type FusionNodeData = {
    label: string;
    nodeType: string;
    config: ServerNode;
    pinned?: boolean;
    breakpoint?: boolean;
    // #406: live per-node badges from the run SSE stream.
    tokens?: number;
    toolCalls?: number;
    runStatus?: string;
};

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
            {data.tokens || data.toolCalls || data.runStatus ? (
                <div className="mt-1 flex items-center gap-1 text-[9px]">
                    {data.tokens ? (
                        <span className="rounded bg-brand/10 px-1 tabular-nums text-brand">
                            {data.tokens} tok
                        </span>
                    ) : null}
                    {data.toolCalls ? (
                        <span className="rounded bg-bg-muted px-1 tabular-nums text-text-secondary">
                            {data.toolCalls} calls
                        </span>
                    ) : null}
                    {data.runStatus === 'error' || data.runStatus === 'failed' ? (
                        <span className="rounded bg-error/15 px-1 text-error">failed</span>
                    ) : data.runStatus === 'done' || data.runStatus === 'result' ? (
                        <span className="rounded bg-success/15 px-1 text-success">ok</span>
                    ) : null}
                </div>
            ) : null}
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

// #398-6c: visual group container — dashed box, no ports, not a runtime node.
function FusionGroup(props: { data: FusionNodeData; selected?: boolean }) {
    const { data, selected } = props;
    return (
        <div
            className={`h-full w-full rounded-xl border-2 border-dashed px-3 py-2 ${
                selected ? 'border-brand bg-brand/5' : 'border-border bg-bg-muted/40'
            }`}
        >
            <div className="text-[10px] font-medium text-text-secondary">🗂 {data.label}</div>
        </div>
    );
}

const nodeTypes: NodeTypes = { fusionNode: FusionNode, fusionGroup: FusionGroup };

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
    const [selectedIds, setSelectedIds] = useState<string[]>([]);
    const [selectedEdgeId, setSelectedEdgeId] = useState('');
    const [toast, setToast] = useState('');
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [govEvents, setGovEvents] = useState<GovEvent[]>([]);
    const [activeNodes, setActiveNodes] = useState<Record<string, string>>({});
    // #406: per-node token/tool-call counters. Token frames arrive per token,
    // so accumulate in a ref and flush to state on a 400 ms tick instead of
    // re-rendering the whole canvas per token.
    const [nodeStats, setNodeStats] = useState<
        Record<string, { tokens: number; toolCalls: number; status?: string }>
    >({});
    const nodeStatsRef = useRef<
        Record<string, { tokens: number; toolCalls: number; status?: string }>
    >({});
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
    const [toolNames, setToolNames] = useState<string[]>([]);
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [rawJsonOpen, setRawJsonOpen] = useState(false);
    const [saveIssues, setSaveIssues] = useState<GraphIssue[]>([]);
    const [strictSave, setStrictSave] = useState(false);
    // #406: save diff guard — card lists what changed before the write.
    const [pendingSave, setPendingSave] = useState<{
        next: GraphJson;
        added: string[];
        removed: string[];
        changed: string[];
        edgesAdded: number;
        edgesRemoved: number;
    } | null>(null);
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
        rpc<{ tools?: Array<{ name: string }> }>('tool.list', {})
            .then((res) => setToolNames((res.tools || []).map((x) => x.name)))
            .catch(() => setToolNames([]));
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
                setSelectedIds([]);
                setSelectedEdgeId('');
                setSaveIssues([]);
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
            const selIds = changes
                .filter((c) => c.type === 'select' && c.selected && 'id' in c)
                .map((c) => (c as { id: string }).id);
            if (selIds.length > 0) {
                setSelectedEdgeId('');
                setSelectedIds((prev) => {
                    const next = new Set(prev);
                    for (const id of selIds) next.add(id);
                    return [...next];
                });
                setSelectedId(selIds[selIds.length - 1]);
            }
            const deselected = changes.filter((c) => c.type === 'select' && !c.selected);
            if (deselected.length > 0) {
                setSelectedIds((prev) => {
                    const removed = new Set(deselected.map((c) => (c as { id: string }).id));
                    return prev.filter((id) => !removed.has(id));
                });
            }
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

    const addNodeAt = useCallback(
        (nodeType: string, position: { x: number; y: number }, connectFrom?: string) => {
            const id = `${nodeType}_${Math.random().toString(36).slice(2, 6)}`;
            const newNode: Node = {
                id,
                type: 'fusionNode',
                position,
                data: { label: id, nodeType, config: { type: nodeType, label: id } },
            };
            pushHistory();
            setNodes((ns) => [...ns, newNode]);
            if (connectFrom) {
                const serverEdges = edges.map((e) => ({
                    source_id: e.source,
                    target_id: e.target,
                }));
                const check = canConnect(graph, connectFrom, id, serverEdges);
                if (check.ok) {
                    setEdges((eds) =>
                        addEdge(
                            {
                                id: `${connectFrom}-${id}`,
                                source: connectFrom,
                                target: id,
                                markerEnd: { type: MarkerType.ArrowClosed },
                            },
                            eds
                        )
                    );
                } else {
                    showToast(
                        `${t('节点已添加但未连线', 'Node added without edge')}: ${check.reason}`
                    );
                }
            }
            return id;
        },
        [edges, graph, pushHistory, setNodes, setEdges, showToast, t]
    );

    const addNodeFromMenu = useCallback(
        (nodeType: string) => {
            if (!connectMenu) return;
            const position = screenToFlowPosition({ x: connectMenu.x, y: connectMenu.y });
            addNodeAt(nodeType, position, connectMenu.from);
            setConnectMenu(null);
        },
        [connectMenu, screenToFlowPosition, addNodeAt]
    );

    const addNode = useCallback(
        (nodeType: string) => {
            addNodeAt(nodeType, { x: 80 + Math.random() * 220, y: 80 + Math.random() * 160 });
        },
        [addNodeAt]
    );

    // #394: Langflow-style drop from the sidebar palette at the cursor position.
    const onDrop = useCallback(
        (event: React.DragEvent) => {
            event.preventDefault();
            const nodeType = event.dataTransfer.getData('fusion/node-type');
            if (!nodeType || !NODE_TYPES.includes(nodeType as (typeof NODE_TYPES)[number])) return;
            const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
            addNodeAt(nodeType, position);
        },
        [screenToFlowPosition, addNodeAt]
    );

    const selectedNode = useMemo(
        () =>
            nodes.find((n) => n.id === selectedId && n.data.nodeType !== 'group') as
                | Node<FusionNodeData>
                | undefined,
        [nodes, selectedId]
    );

    const selectedEdge = useMemo(
        () => edges.find((e) => e.id === selectedEdgeId),
        [edges, selectedEdgeId]
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

    // #393: single-field update — no history push per keystroke, only on blur
    // commit (history pushed once per edit session via commitField).
    const setFieldValue = useCallback(
        (key: string, value: unknown) => {
            if (!selectedId) return;
            setNodes((ns) =>
                ns.map((n) =>
                    n.id === selectedId
                        ? {
                              ...n,
                              data: {
                                  ...n.data,
                                  config: {
                                      ...(n.data as FusionNodeData).config,
                                      [key]: value,
                                  },
                              },
                          }
                        : n
                )
            );
        },
        [selectedId, setNodes]
    );

    const commitField = useCallback(() => {
        pushHistory();
    }, [pushHistory]);

    const removeSelected = useCallback(() => {
        if (!selectedId) return;
        pushHistory();
        setNodes((ns) => ns.filter((n) => n.id !== selectedId));
        setEdges((es) => es.filter((e) => e.source !== selectedId && e.target !== selectedId));
        setSelectedId('');
    }, [selectedId, setNodes, setEdges, pushHistory]);

    const deleteSelectedEdge = useCallback(() => {
        if (!selectedEdgeId) return;
        pushHistory();
        setEdges((es) => es.filter((e) => e.id !== selectedEdgeId));
        setSelectedEdgeId('');
    }, [selectedEdgeId, setEdges, pushHistory]);

    // #398-6c: wrap currently selected nodes in a visual group container.
    const groupSelected = useCallback(() => {
        const members = nodes.filter(
            (n) => selectedIds.includes(n.id) && (n.data as FusionNodeData).nodeType !== 'group'
        );
        if (members.length < 1) {
            showToast(t('先框选要分组的节点', 'Select nodes to group first'));
            return;
        }
        const groupId = `group_${Math.random().toString(36).slice(2, 6)}`;
        const minX = Math.min(...members.map((n) => n.position.x)) - 40;
        const minY = Math.min(...members.map((n) => n.position.y)) - 56;
        const maxX = Math.max(...members.map((n) => n.position.x + 160));
        const maxY = Math.max(...members.map((n) => n.position.y + 80));
        pushHistory();
        setNodes((ns) => [
            ...ns.map((n) =>
                members.some((m) => m.id === n.id)
                    ? {
                          ...n,
                          parentId: groupId,
                          extent: 'parent' as const,
                          position: { x: n.position.x - minX, y: n.position.y - minY },
                      }
                    : n
            ),
            {
                id: groupId,
                type: 'fusionGroup',
                position: { x: minX, y: minY },
                style: { width: maxX - minX + 40, height: maxY - minY + 40 },
                data: {
                    label: groupId,
                    nodeType: 'group',
                    config: { type: 'group', label: groupId },
                },
            },
        ]);
        setSelectedIds([]);
    }, [nodes, selectedIds, setNodes, pushHistory, showToast, t]);

    const locateIssue = useCallback(
        (issue: GraphIssue) => {
            if (!issue.nodeId) return;
            setSelectedId(issue.nodeId);
            setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === issue.nodeId })));
        },
        [setNodes]
    );

    // #395: client pre-save check, then server-side validate (soft by default;
    // response issues surfaced in the save-issues panel).
    const persist = useCallback(
        async (next: GraphJson) => {
            if (!graphId) return;
            setSaving(true);
            try {
                const res = await rpc<{ issues?: string[] }>('graph.update', {
                    graph_id: graphId,
                    graph_data: next,
                    strict_validate: strictSave,
                });
                setGraph(next);
                const serverIssues = (res.issues || []).map((msg) => ({
                    nodeId: '',
                    level: (msg.startsWith('warning:') ? 'warning' : 'error') as
                        | 'warning'
                        | 'error',
                    message: msg,
                }));
                setSaveIssues(serverIssues);
                showToast(
                    serverIssues.length > 0
                        ? `${t('已保存', 'Saved')} · ${t('条校验提示', 'validation notes')}: ${serverIssues.length}`
                        : t('已保存', 'Saved')
                );
            } catch (e) {
                showToast(`${t('保存失败', 'Save failed')}: ${String(e)}`);
            } finally {
                setSaving(false);
            }
        },
        [graphId, strictSave, showToast, t]
    );

    const save = useCallback(async () => {
        if (!graphId) return;
        const next = fromRf(graph, nodes as unknown as RfNode[], edges as unknown as RfEdge[]);
        const local = validateGraphClient(next);
        const hard = local.filter((i) => i.level === 'error');
        if (hard.length > 0) {
            setSaveIssues(local);
            locateIssue(hard[0]);
            showToast(`${t('保存被拦截', 'Save blocked')}: ${hard[0].message}`);
            return;
        }
        // #406: diff current canvas against the last saved graph; an empty diff
        // saves straight through, a non-empty one opens the diff card.
        const prevNodes = graph.nodes ?? {};
        const nextNodes = next.nodes ?? {};
        const prevIds = Object.keys(prevNodes);
        const nextIds = Object.keys(nextNodes);
        const added = nextIds.filter((id) => !(id in prevNodes));
        const removed = prevIds.filter((id) => !(id in nextNodes));
        const changed = nextIds.filter(
            (id) =>
                id in prevNodes && JSON.stringify(prevNodes[id]) !== JSON.stringify(nextNodes[id])
        );
        const keyOf = (e: { source_id: string; target_id: string; label?: string }) =>
            `${e.source_id}>${e.target_id}>${e.label ?? ''}`;
        const prevEdges = new Set((graph.edges ?? []).map(keyOf));
        const nextEdges = new Set((next.edges ?? []).map(keyOf));
        const edgesAdded = [...nextEdges].filter((k) => !prevEdges.has(k)).length;
        const edgesRemoved = [...prevEdges].filter((k) => !nextEdges.has(k)).length;
        if (!added.length && !removed.length && !changed.length && !edgesAdded && !edgesRemoved) {
            await persist(next);
            return;
        }
        setPendingSave({ next, added, removed, changed, edgesAdded, edgesRemoved });
    }, [graphId, graph, nodes, edges, persist, showToast, t, locateIssue]);

    // #396: export current graph JSON; import via file picker -> graph.create.
    const exportGraph = useCallback(async () => {
        if (!graphId) return;
        const next = fromRf(graph, nodes as unknown as RfNode[], edges as unknown as RfEdge[]);
        const blob = new Blob([JSON.stringify(next, null, 4)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${graph.name || graphId}.json`;
        a.click();
        URL.revokeObjectURL(url);
    }, [graphId, graph, nodes, edges]);

    const importGraph = useCallback(
        async (data: GraphJson) => {
            try {
                const res = await rpc<{ graph_id?: string }>('graph.create', {
                    name: data.name || 'imported graph',
                    description: data.description || '',
                    graph_data: data,
                });
                const newId = res.graph_id || '';
                if (newId) {
                    const refreshed = await api.graphs();
                    setGraphs(refreshed.data.map((g) => ({ graph_id: g.graph_id, name: g.name })));
                    setGraphId(newId);
                    void loadGraph(newId);
                }
                showToast(t('图已导入', 'Graph imported'));
            } catch (e) {
                showToast(`${t('导入失败', 'Import failed')}: ${String(e)}`);
            }
        },
        [loadGraph, showToast, t]
    );

    const onImportFile = useCallback(
        (ev: React.ChangeEvent<HTMLInputElement>) => {
            const file = ev.target.files?.[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => {
                try {
                    void importGraph(JSON.parse(String(reader.result)) as GraphJson);
                } catch {
                    showToast(t('JSON 解析失败', 'Invalid JSON'));
                }
            };
            reader.readAsText(file);
            ev.target.value = '';
        },
        [importGraph, showToast, t]
    );

    const runWithStream = useCallback(
        async (opts: { fromNode?: string; onlyNode?: string } = {}) => {
            if (!graphId || running) return;
            setRunning(true);
            setGovEvents([]);
            setActiveNodes({});
            nodeStatsRef.current = {};
            setNodeStats({});
            void refreshGovernance();
            const qs = new URLSearchParams({ input: 'canvas run' });
            if (opts.fromNode) qs.set('from_node', opts.fromNode);
            if (opts.onlyNode) qs.set('only_node', opts.onlyNode);
            const breakpointNode = nodes.find((n) => (n.data as FusionNodeData).breakpoint);
            if (breakpointNode && !opts.onlyNode) qs.set('stop_after', breakpointNode.id);
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
                        setNodeStats({ ...nodeStatsRef.current });
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
                        const stats = nodeStatsRef.current[nodeId] ?? {
                            tokens: 0,
                            toolCalls: 0,
                        };
                        if (kind === 'token' || kind === 'thinking_token') stats.tokens += 1;
                        if (kind === 'tool_call') stats.toolCalls += 1;
                        if (kind === 'error' || kind === 'failed') stats.status = 'error';
                        else if (kind === 'result' || kind === 'done') stats.status = 'done';
                        nodeStatsRef.current[nodeId] = stats;
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

    // #406: coalesce per-node counter updates (token frames are per-token).
    useEffect(() => {
        if (!running) return;
        const timer = window.setInterval(() => setNodeStats({ ...nodeStatsRef.current }), 400);
        return () => window.clearInterval(timer);
    }, [running]);

    const displayNodes = useMemo(
        () =>
            Object.keys(nodeStats).length === 0
                ? nodes
                : nodes.map((n) => {
                      const s = nodeStats[n.id];
                      if (!s) return n;
                      return {
                          ...n,
                          data: {
                              ...n.data,
                              tokens: s.tokens,
                              toolCalls: s.toolCalls,
                              runStatus: s.status,
                          },
                      };
                  }),
        [nodes, nodeStats]
    );

    const runTotals = useMemo(() => {
        const values = Object.values(nodeStats);
        return {
            tokens: values.reduce((a, s) => a + s.tokens, 0),
            toolCalls: values.reduce((a, s) => a + s.toolCalls, 0),
            nodes: values.length,
        };
    }, [nodeStats]);

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

    // #393: one generated form row per FieldSpec widget.
    const renderField = (field: FieldSpec) => {
        if (!selectedNode) return null;
        const config = selectedNode.data.config;
        const value = config[field.key] ?? field.default;
        const label = (
            <div className="text-text-secondary">
                {field.help
                    ? `${t(field.label, field.label)} (${field.help})`
                    : t(field.label, field.label)}
            </div>
        );
        if (field.widget === 'textarea') {
            return (
                <div key={field.key}>
                    {label}
                    <textarea
                        className="w-full h-20 rounded border border-border bg-bg px-2 py-1 font-mono"
                        defaultValue={String(value ?? '')}
                        onBlur={(e) => {
                            setFieldValue(field.key, e.target.value);
                            commitField();
                        }}
                    />
                </div>
            );
        }
        if (field.widget === 'number') {
            return (
                <div key={field.key}>
                    {label}
                    <input
                        type="number"
                        className="w-full rounded border border-border bg-bg px-2 py-1"
                        value={Number(value ?? 0)}
                        min={field.min}
                        max={field.max}
                        step={field.step}
                        onChange={(e) => setFieldValue(field.key, Number(e.target.value))}
                        onBlur={commitField}
                    />
                </div>
            );
        }
        if (field.widget === 'select') {
            return (
                <div key={field.key}>
                    {label}
                    <select
                        className="w-full rounded border border-border bg-bg px-2 py-1"
                        value={String(value ?? '')}
                        onChange={(e) => {
                            setFieldValue(field.key, e.target.value);
                            commitField();
                        }}
                    >
                        {(field.options || []).map((o) => (
                            <option key={o} value={o}>
                                {o || '—'}
                            </option>
                        ))}
                    </select>
                </div>
            );
        }
        if (field.widget === 'boolean') {
            return (
                <label key={field.key} className="flex items-center gap-1">
                    <input
                        type="checkbox"
                        checked={Boolean(value)}
                        onChange={(e) => {
                            setFieldValue(field.key, e.target.checked);
                            commitField();
                        }}
                    />
                    {t(field.label, field.label)}
                </label>
            );
        }
        if (field.widget === 'tags') {
            const tags = Array.isArray(value) ? (value as string[]) : [];
            return (
                <div key={field.key}>
                    {label}
                    <input
                        className="w-full rounded border border-border bg-bg px-2 py-1"
                        value={tags.join(', ')}
                        placeholder="a, b, c"
                        onBlur={(e) => {
                            const list = e.target.value
                                .split(',')
                                .map((s) => s.trim())
                                .filter(Boolean);
                            setFieldValue(field.key, list);
                            commitField();
                        }}
                    />
                </div>
            );
        }
        if (field.widget === 'kv') {
            return (
                <div key={field.key}>
                    {label}
                    <textarea
                        className="w-full h-16 rounded border border-border bg-bg px-2 py-1 font-mono"
                        defaultValue={JSON.stringify(value ?? {}, null, 4)}
                        onBlur={(e) => {
                            try {
                                const parsed = JSON.parse(e.target.value || '{}');
                                setFieldValue(field.key, parsed);
                                commitField();
                            } catch {
                                showToast(t('JSON 解析失败', 'Invalid JSON'));
                            }
                        }}
                    />
                </div>
            );
        }
        if (field.widget === 'text' && field.key === 'tool_name' && toolNames.length > 0) {
            return (
                <div key={field.key}>
                    {label}
                    <input
                        className="w-full rounded border border-border bg-bg px-2 py-1"
                        list="canvas-tool-names"
                        value={String(value ?? '')}
                        onChange={(e) => setFieldValue(field.key, e.target.value)}
                        onBlur={commitField}
                    />
                    <datalist id="canvas-tool-names">
                        {toolNames.map((name) => (
                            <option key={name} value={name} />
                        ))}
                    </datalist>
                </div>
            );
        }
        return (
            <div key={field.key}>
                {label}
                <input
                    className="w-full rounded border border-border bg-bg px-2 py-1"
                    value={String(value ?? '')}
                    onChange={(e) => setFieldValue(field.key, e.target.value)}
                    onBlur={commitField}
                />
            </div>
        );
    };

    const basicFields = selectedNode ? fieldsForNode(selectedNode.data.nodeType, false) : [];
    const advFields = selectedNode ? fieldsForNode(selectedNode.data.nodeType, true) : [];

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
                {NODE_GROUPS.map((grp) => (
                    <div key={grp.title}>
                        <div className="text-xs text-text-secondary pt-1">
                            {t(grp.title, grp.title)}
                        </div>
                        <div className="grid grid-cols-2 gap-1">
                            {grp.types.map((nt) => (
                                <button
                                    type="button"
                                    key={nt}
                                    draggable
                                    onDragStart={(e) => {
                                        e.dataTransfer.setData('fusion/node-type', nt);
                                        e.dataTransfer.effectAllowed = 'copy';
                                    }}
                                    className="rounded border border-border px-1 py-0.5 text-[10px] hover:border-brand cursor-grab"
                                    onClick={() => addNode(nt)}
                                    title={t(
                                        '点击添加，或拖到画布任意位置',
                                        'Click to add, or drag onto the canvas'
                                    )}
                                >
                                    + {nt}
                                </button>
                            ))}
                        </div>
                    </div>
                ))}
                <button
                    type="button"
                    className="w-full rounded border border-border px-2 py-1 text-[10px] disabled:opacity-40"
                    disabled={selectedIds.length === 0}
                    onClick={groupSelected}
                >
                    🗂 {t('分组选中节点', 'Group selected nodes')}
                </button>
                <button
                    type="button"
                    className="w-full rounded bg-brand px-2 py-1 text-xs text-white disabled:opacity-50 mt-2"
                    disabled={!graphId || saving}
                    onClick={() => void save()}
                >
                    {saving ? t('保存中…', 'Saving…') : t('保存', 'Save')}
                </button>
                <label className="flex items-center gap-1 text-[10px]">
                    <input
                        type="checkbox"
                        checked={strictSave}
                        onChange={(e) => setStrictSave(e.target.checked)}
                    />
                    {t('严格校验 (错误阻止保存)', 'Strict validate (errors block save)')}
                </label>
                <button
                    type="button"
                    className="w-full rounded border border-brand px-2 py-1 text-xs text-brand disabled:opacity-50"
                    disabled={!graphId || running}
                    onClick={() => void runWithStream()}
                >
                    {running ? t('运行中…', 'Running…') : t('运行并监视', 'Run & watch')}
                </button>
                {runTotals.nodes > 0 ? (
                    <div className="rounded border border-border bg-bg-elevated px-2 py-1 text-[10px] text-text-secondary">
                        {t('本次运行', 'This run')}:{' '}
                        <span className="tabular-nums text-brand">{runTotals.tokens}</span> tok ·{' '}
                        <span className="tabular-nums">{runTotals.toolCalls}</span>{' '}
                        {t('工具调用', 'tool calls')} · {runTotals.nodes} {t('节点', 'nodes')}
                    </div>
                ) : null}
                <div className="flex gap-1 pt-1">
                    <button
                        type="button"
                        className="flex-1 rounded border border-border px-1 py-0.5 text-[10px] disabled:opacity-40"
                        disabled={!graphId}
                        onClick={() => void exportGraph()}
                    >
                        ⬇ {t('导出', 'Export')}
                    </button>
                    <label className="flex-1 rounded border border-border px-1 py-0.5 text-[10px] text-center cursor-pointer hover:border-brand">
                        ⬆ {t('导入', 'Import')}
                        <input
                            type="file"
                            accept=".json"
                            className="hidden"
                            onChange={onImportFile}
                        />
                    </label>
                </div>
                {TEMPLATES.length > 0 ? (
                    <div>
                        <div className="text-xs text-text-secondary pt-1">
                            {t('模板', 'Templates')}
                        </div>
                        <div className="space-y-0.5">
                            {TEMPLATES.map((tpl) => (
                                <button
                                    type="button"
                                    key={tpl.name}
                                    className="block w-full rounded border border-border px-1 py-0.5 text-left text-[10px] hover:border-brand"
                                    onClick={() => void importGraph(tpl.graph)}
                                >
                                    {tpl.name.replace(/\.json$/, '')}
                                </button>
                            ))}
                        </div>
                    </div>
                ) : null}
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
                    nodes={displayNodes}
                    edges={edges}
                    nodeTypes={nodeTypes}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onConnect={guardConnect}
                    onConnectEnd={onConnectEnd}
                    onNodeDragStart={pushHistory}
                    onEdgeClick={(_, edge) => {
                        setSelectedEdgeId(edge.id);
                        setSelectedId('');
                    }}
                    onPaneClick={() => {
                        setSelectedEdgeId('');
                        setSelectedIds([]);
                    }}
                    onDragOver={(e) => {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = 'copy';
                    }}
                    onDrop={onDrop}
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
                {selectedEdge ? (
                    <div className="space-y-2 text-xs">
                        <div className="text-text-secondary">
                            {t('边', 'Edge')} {selectedEdge.source} → {selectedEdge.target}
                        </div>
                        <div>
                            <div className="text-text-secondary">
                                {t(
                                    '分支标签 (condition 分支名)',
                                    'Branch label (condition branch)'
                                )}
                            </div>
                            <input
                                className="w-full rounded border border-border bg-bg px-2 py-1"
                                value={String(selectedEdge.label ?? '')}
                                onBlur={(e) => {
                                    const label = e.target.value;
                                    if (label === (selectedEdge.label || '')) return;
                                    pushHistory();
                                    setEdges((es) =>
                                        es.map((e) =>
                                            e.id === selectedEdge.id
                                                ? { ...e, label: label || undefined }
                                                : e
                                        )
                                    );
                                }}
                            />
                        </div>
                        <button
                            type="button"
                            className="rounded border border-error px-2 py-1 text-error"
                            onClick={deleteSelectedEdge}
                        >
                            {t('删除边', 'Delete edge')}
                        </button>
                    </div>
                ) : selectedNode ? (
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
                        {basicFields.map(renderField)}
                        {advFields.length > 0 ? (
                            <div>
                                <button
                                    type="button"
                                    className="text-[10px] text-brand"
                                    onClick={() => setAdvancedOpen((v) => !v)}
                                >
                                    {advancedOpen ? '▾' : '▸'} {t('高级参数', 'Advanced')}
                                </button>
                                {advancedOpen ? (
                                    <div className="space-y-2 pt-1">
                                        {advFields.map(renderField)}
                                    </div>
                                ) : null}
                            </div>
                        ) : null}
                        <div>
                            <button
                                type="button"
                                className="text-[10px] text-brand"
                                onClick={() => setRawJsonOpen((v) => !v)}
                            >
                                {rawJsonOpen ? '▾' : '▸'} {t('原始 JSON', 'Raw JSON')}
                            </button>
                            {rawJsonOpen ? (
                                <textarea
                                    className="mt-1 w-full h-40 rounded border border-border bg-bg px-2 py-1 font-mono"
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
                            ) : null}
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
                            className="w-full rounded border border-brand px-2 py-1 text-brand disabled:opacity-40"
                            disabled={running || !graphId}
                            onClick={() => void runWithStream({ onlyNode: selectedNode.id })}
                        >
                            {t('仅试跑此节点', 'Run this node only')}
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
                        {t('点击节点或边以编辑', 'Click a node or edge to inspect')}
                    </div>
                )}
                {saveIssues.length > 0 ? (
                    <div className="rounded border border-warning/40 bg-bg-elevated p-1 text-[10px]">
                        <div className="text-text-secondary">
                            {t('校验提示', 'Validation notes')} ({saveIssues.length})
                        </div>
                        {saveIssues.map((iss, i) => (
                            <button
                                type="button"
                                key={i}
                                className={`block w-full text-left ${iss.level === 'error' ? 'text-error' : 'text-text-secondary'}`}
                                onClick={() => locateIssue(iss)}
                            >
                                · {iss.message}
                            </button>
                        ))}
                    </div>
                ) : null}
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
            <ConfirmDialog
                open={pendingSave !== null}
                title={t('保存前确认改动', 'Confirm canvas changes')}
                description={
                    pendingSave
                        ? [
                              pendingSave.added.length
                                  ? `${t('新增节点', 'Added nodes')}: ${pendingSave.added.join(', ')}`
                                  : '',
                              pendingSave.removed.length
                                  ? `${t('删除节点', 'Removed nodes')}: ${pendingSave.removed.join(', ')}`
                                  : '',
                              pendingSave.changed.length
                                  ? `${t('修改节点', 'Changed nodes')}: ${pendingSave.changed.join(', ')}`
                                  : '',
                              pendingSave.edgesAdded || pendingSave.edgesRemoved
                                  ? `${t('连线', 'Edges')}: +${pendingSave.edgesAdded} / -${pendingSave.edgesRemoved}`
                                  : '',
                          ]
                              .filter(Boolean)
                              .join(' · ')
                        : ''
                }
                confirmLabel={t('确认保存', 'Save changes')}
                cancelLabel={t('返回编辑', 'Back to edit')}
                onConfirm={() => {
                    const next = pendingSave?.next;
                    setPendingSave(null);
                    if (next) void persist(next);
                }}
                onCancel={() => setPendingSave(null)}
            />
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
