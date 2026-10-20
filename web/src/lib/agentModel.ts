// #413: legacy agent manifests store model as an object
// ({provider, name, temperature, max_tokens}) while newer ones store a plain
// string. Rendering the object directly as a React child throws #31 and
// unmounts the page, so every model surface must normalize first.
export interface AgentModelConfig {
    provider?: string;
    name?: string;
    temperature?: number;
    max_tokens?: number;
}

export type AgentModel = string | AgentModelConfig | null | undefined;

export function modelLabel(model: AgentModel): string {
    if (!model) return '-';
    if (typeof model === 'string') return model || '-';
    return model.name || '-';
}
