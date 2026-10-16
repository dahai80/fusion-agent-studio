// Multi-line text input (web-ui only ships TextInput).
export function Textarea(props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
    rows?: number;
    mono?: boolean;
    id?: string;
}) {
    const { value, onChange, placeholder, rows = 4, mono = false, id } = props;
    return (
        <textarea
            id={id}
            value={value}
            rows={rows}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
            className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-brand ${
                mono ? 'font-mono text-xs' : ''
            }`}
        />
    );
}
