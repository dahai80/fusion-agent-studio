import { useState } from 'react';
import { Textarea } from './Textarea';

// JSON textarea with parse validation — used for graph_data / params / config
// bodies. Validation runs on blur; parent submits only valid JSON via
// parseJson.
export function JsonEditor(props: {
    value: string;
    onChange: (value: string) => void;
    rows?: number;
    placeholder?: string;
}) {
    const { value, onChange, rows = 8, placeholder } = props;
    const [error, setError] = useState<string | null>(null);
    const validate = () => {
        if (!value.trim()) {
            setError(null);
            return;
        }
        try {
            JSON.parse(value);
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    };
    return (
        <div className="flex flex-col gap-1">
            <Textarea
                value={value}
                onChange={onChange}
                rows={rows}
                mono
                placeholder={placeholder}
            />
            {error ? <span className="text-xs text-error">{error}</span> : null}
            <button
                type="button"
                onClick={validate}
                className="self-start text-xs text-text-secondary hover:text-text-primary"
            >
                校验 JSON
            </button>
        </div>
    );
}

export function parseJson(
    text: string
): { ok: true; data: unknown } | { ok: false; error: string } {
    if (!text.trim()) return { ok: true, data: {} };
    try {
        return { ok: true, data: JSON.parse(text) };
    } catch (e) {
        return { ok: false, error: (e as Error).message };
    }
}
