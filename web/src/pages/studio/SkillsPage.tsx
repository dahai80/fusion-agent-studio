import {
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
    SkeletonLines,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Skills (SkillTab equivalent): agent-scoped skill.execute + adaptive
// research.adaptive — result rendered as pretty JSON.
interface AgentRow {
    id?: string;
    agent_id?: string;
    name?: string;
}

export function SkillsPage() {
    const t = useT();
    return (
        <div>
            <PageHeader title={t('技能', 'Skills')} />
            <div className="pt-3">
                <SkillSection />
            </div>
            <div className="pt-3">
                <ResearchSection />
            </div>
        </div>
    );
}

function SkillSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ agents: AgentRow[] }>('agent.list', {}),
        []
    );
    const [agentId, setAgentId] = useState('');
    const [skillName, setSkillName] = useState('');
    const [input, setInput] = useState('');
    const [result, setResult] = useState('');
    const [busy, setBusy] = useState(false);

    const agents = data?.agents ?? [];
    const agentOptions = agents.map((a) => ({
        value: a.id ?? a.agent_id ?? '',
        label: a.name || a.id || a.agent_id || '',
    }));
    const options =
        agentOptions.length > 0
            ? agentOptions
            : [{ value: '', label: t('暂无 Agent', 'no agents') }];

    const run = async () => {
        if (!agentId || !skillName.trim()) return;
        setBusy(true);
        try {
            const res = await rpc<{ result: unknown }>('skill.execute', {
                agent_id: agentId,
                skill_name: skillName.trim(),
                input: input,
            });
            setResult(JSON.stringify(res.result ?? res, null, 2));
            sdk.toast.show({ level: 'success', message: t('执行完成', 'done') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <div className="max-w-lg px-4">
                {loading && attempt === 0 && agents.length === 0 ? (
                    <SkeletonLines rows={3} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : agents.length === 0 ? (
                    <EmptyState title={t('暂无 Agent', 'no agents')} />
                ) : null}
                <div className="mt-3 flex flex-col gap-3">
                    <FormField label={t('Agent', 'Agent')} required>
                        <SelectInput value={agentId} onChange={setAgentId} options={options} />
                    </FormField>
                    <FormField label={t('技能名称', 'Skill name')} required>
                        <TextInput value={skillName} onChange={setSkillName} />
                    </FormField>
                    <FormField label={t('输入', 'Input')}>
                        <Textarea value={input} onChange={setInput} rows={4} />
                    </FormField>
                    <div>
                        <SubmitButton label={t('执行', 'Execute')} onAction={run} disabled={busy} />
                    </div>
                </div>
            </div>
            {result ? (
                <pre className="mx-4 mt-3 max-h-96 overflow-auto rounded-md bg-bg-muted p-3 font-mono text-xs">
                    {result}
                </pre>
            ) : null}
        </div>
    );
}

function ResearchSection() {
    const t = useT();
    const [question, setQuestion] = useState('');
    const [result, setResult] = useState('');
    const [busy, setBusy] = useState(false);

    const run = async () => {
        if (!question.trim()) return;
        setBusy(true);
        try {
            const res = await rpc<{ result: unknown }>('research.adaptive', {
                question: question.trim(),
            });
            setResult(JSON.stringify(res.result ?? res, null, 2));
            sdk.toast.show({ level: 'success', message: t('执行完成', 'done') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <div className="max-w-lg flex flex-col gap-3 px-4">
                <div className="text-sm font-medium">{t('自适应研究', 'Adaptive research')}</div>
                <FormField label={t('问题', 'Question')} required>
                    <Textarea value={question} onChange={setQuestion} rows={3} />
                </FormField>
                <div>
                    <SubmitButton label={t('执行', 'Execute')} onAction={run} disabled={busy} />
                </div>
            </div>
            {result ? (
                <pre className="mx-4 mt-3 max-h-96 overflow-auto rounded-md bg-bg-muted p-3 font-mono text-xs">
                    {result}
                </pre>
            ) : null}
        </div>
    );
}
